from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from catalog.models import Institution, Site
from matching import engine
from matching.engine import Preferences

_QUERY = 'минеральные ванны'
_SCORE_TODO = (
    '# TODO(team): score = w_procedures * процедуры + w_excursions * экскурсии '
    '+ w_transport * транспорт + w_price * цена. Коэффициенты задаём сами.'
)
_PROMPT_TODO = '# TODO(team): текст системного промпта пишем сами.'


class MappingEncoder:
    def __init__(self, vectors):
        self.vectors = vectors
        self.texts = []

    def encode(self, texts):
        self.texts = list(texts)
        return [self.vectors[text] for text in texts]


def _cosine(left, right):
    return engine._cosine(left, right)


class PreferenceTests(SimpleTestCase):
    def test_normalizes_budget_region_and_query(self):
        preferences = Preferences(budget='8000.50', region='  Крым ', query=' ванны ')

        self.assertEqual(preferences.budget, Decimal('8000.50'))
        self.assertEqual(preferences.region, 'Крым')
        self.assertEqual(preferences.query, 'ванны')

    def test_rejects_negative_budget(self):
        with self.assertRaises(ValueError):
            Preferences(budget='-1', region='Крым', query='ванны')

    def test_rejects_non_string_region(self):
        with self.assertRaises(TypeError):
            Preferences(budget=1, region=None, query='ванны')


class SiteTextTests(SimpleTestCase):
    def test_joins_procedures_excursions_and_description(self):
        site = Site(
            procedures='  ванны ',
            excursions='море',
            institution=Institution(description=' Описание '),
        )

        self.assertEqual(engine.site_text(site), 'ванны\nморе\nОписание')

    def test_skips_blank_parts(self):
        site = Site(
            procedures='  ',
            excursions='',
            institution=Institution(description='Описание'),
        )

        self.assertEqual(engine.site_text(site), 'Описание')


class TeamMarkerTests(SimpleTestCase):
    def test_score_formula_and_system_prompt_are_left_for_the_team(self):
        source = Path(engine.__file__).read_text(encoding='utf-8')

        formula_at = source.index(_SCORE_TODO)
        score_at = source.index('score = similarity')
        prompt_at = source.index(_PROMPT_TODO)
        complete_at = source.index('llm_client.complete(')

        self.assertLess(formula_at, score_at)
        self.assertLess(prompt_at, complete_at)
        self.assertEqual(engine.EXPLAIN_SYSTEM_PROMPT, '')


class ExplainTests(SimpleTestCase):
    @patch('matching.engine.llm_client.complete', return_value='подходит')
    def test_calls_llm_client_with_empty_system_prompt(self, complete):
        institution = Institution(
            name='Ключи',
            description='Парк и источники',
            treatment_profile='Общий',
        )
        site = Site(
            institution=institution,
            address='Крым, Ялта',
            procedures='ванны',
            excursions='ласточкино гнездо',
            transport_accessibility=4,
            min_daily_price=Decimal('4500.00'),
            max_daily_price=Decimal('9200.00'),
        )
        preferences = Preferences(budget=Decimal('8000'), region='Крым', query='ванны у моря')

        self.assertEqual(engine.explain(preferences, site), 'подходит')
        system, user = complete.call_args.args
        self.assertEqual(system, engine.EXPLAIN_SYSTEM_PROMPT)
        self.assertIn('8000', user)
        self.assertIn('ванны у моря', user)
        self.assertIn('Парк и источники', user)
        self.assertIn('ласточкино гнездо', user)
        self.assertIn('Ключи', user)


class MatchTests(TestCase):
    def make_site(
        self,
        address,
        min_price,
        procedures,
        *,
        description='Описание учреждения',
        excursions='Экскурсия',
        max_price=None,
        institution=None,
    ):
        if institution is None:
            institution = Institution.objects.create(
                name=address,
                description=description,
                treatment_profile='Общий профиль',
            )
        return Site.objects.create(
            institution=institution,
            address=address,
            latitude=Decimal('44.500000'),
            longitude=Decimal('34.160000'),
            transport_accessibility=3,
            min_daily_price=Decimal(min_price),
            max_daily_price=Decimal(max_price if max_price is not None else min_price),
            procedures=procedures,
            excursions=excursions,
        )

    def test_filters_then_returns_top_five_by_cosine(self):
        ranked = [
            ('Крым, Алушта', '1000', 'ванны', [1.0, 0.0]),
            ('Крым, Ялта', '2000', 'грязи', [3.0, 1.0]),
            ('город Ялта, Крым', '3000', 'бассейн', [1.0, 1.0]),
            ('Крым, Евпатория', '4000', 'ингаляции', [1.0, 3.0]),
            ('Крым, Саки', '5000', 'лфк', [0.0, 1.0]),
            ('Крым, Судак', '6000', 'массаж', [-1.0, 0.0]),
        ]
        included = [
            self.make_site(address, price, procedures)
            for address, price, procedures, _vector in ranked
        ]
        excluded_region = self.make_site('Сочи, центр', '1000', 'ванны сочи')
        excluded_budget = self.make_site('Крым, Форос', '9000', 'ванны форос', max_price='12000')
        exact_budget = self.make_site(
            'Республика Крым, Гурзуф',
            '8000.00',
            'климат',
            max_price='12000',
        )
        vectors = {_QUERY: [1.0, 0.0]}
        expected = []
        for site, (_address, _price, _procedures, vector) in zip(included, ranked, strict=True):
            vectors[engine.site_text(site)] = vector
            expected.append((site, _cosine([1.0, 0.0], vector)))
        vectors[engine.site_text(exact_budget)] = [0.0, -1.0]
        expected.append((exact_budget, _cosine([1.0, 0.0], [0.0, -1.0])))
        expected.sort(key=lambda item: (-item[1], item[0].pk))
        encoder = MappingEncoder(vectors)

        result = engine.match(
            Preferences(budget='8000', region='кРыМ', query=f'  {_QUERY}  '),
            encoder=encoder,
        )

        self.assertEqual([item.site.pk for item in result], [site.pk for site, _score in expected[:5]])
        for item, (_site, score) in zip(result, expected[:5], strict=True):
            self.assertAlmostEqual(item.score, score)
        returned_ids = {item.site.pk for item in result}
        self.assertNotIn(excluded_region.pk, returned_ids)
        self.assertNotIn(excluded_budget.pk, returned_ids)
        self.assertNotIn(engine.site_text(excluded_region), encoder.texts)
        self.assertNotIn(engine.site_text(excluded_budget), encoder.texts)
        self.assertEqual(encoder.texts[0], _QUERY)
        self.assertIn(engine.site_text(included[0]), encoder.texts)

    def test_equal_scores_keep_smaller_pk_first(self):
        first = self.make_site('Крым, Ялта', '1000', 'ванны ялта')
        second = self.make_site('Крым, Алушта', '1000', 'ванны алушта')
        vectors = {
            _QUERY: [1.0, 0.0],
            engine.site_text(first): [1.0, 0.0],
            engine.site_text(second): [2.0, 0.0],
        }

        result = engine.match(
            Preferences(budget=5000, region='Крым', query=_QUERY),
            encoder=MappingEncoder(vectors),
        )

        self.assertEqual([item.site.pk for item in result], [first.pk, second.pk])
        self.assertAlmostEqual(result[0].score, 1.0)
        self.assertAlmostEqual(result[1].score, 1.0)

    def test_blank_region_keeps_every_affordable_site(self):
        crimea = self.make_site('Крым, Ялта', '1000', 'ванны')
        sochi = self.make_site('Сочи, центр', '1000', 'море')
        vectors = {
            _QUERY: [1.0, 0.0],
            engine.site_text(crimea): [1.0, 0.0],
            engine.site_text(sochi): [0.0, 1.0],
        }

        result = engine.match(
            Preferences(budget=5000, region='   ', query=_QUERY),
            encoder=MappingEncoder(vectors),
        )

        self.assertEqual({item.site.pk for item in result}, {crimea.pk, sochi.pk})

    def test_no_candidates_does_not_encode(self):
        self.make_site('Сочи, центр', '1000', 'море')

        class Boom:
            def encode(self, texts):
                raise AssertionError(texts)

        result = engine.match(
            Preferences(budget=5000, region='Антарктида', query=_QUERY),
            encoder=Boom(),
        )

        self.assertEqual(result, [])

    @patch('matching.engine.llm_client.embed')
    def test_embed_via_api_passes_texts_correctly(self, embed):
        site = self.make_site(
            'Крым, Ялта',
            '1000',
            'ванны',
            excursions='набережная',
            description='Парк у моря',
        )
        embed.return_value = [[1.0, 0.0], [1.0, 0.0]]

        result = engine.match(Preferences(budget=5000, region='Крым', query='ванны'))

        embed.assert_called_once()
        self.assertEqual(
            embed.call_args.args[0],
            ['ванны', 'ванны\nнабережная\nПарк у моря'],
        )
        self.assertEqual(result[0].site.pk, site.pk)
        self.assertAlmostEqual(result[0].score, 1.0)

    @patch('matching.engine.llm_client.complete')
    def test_filter_drops_site_above_budget(self, complete):
        kept = self.make_site('Крым, Ялта', '8000.00', 'ванны', max_price='9000')
        dropped = self.make_site('Крым, Алушта', '8000.01', 'грязи', max_price='9000')
        vectors = {
            _QUERY: [1.0, 0.0],
            engine.site_text(kept): [1.0, 0.0],
            engine.site_text(dropped): [1.0, 0.0],
        }
        encoder = MappingEncoder(vectors)

        result = engine.match(
            Preferences(budget='8000', region='Крым', query=_QUERY),
            encoder=encoder,
        )

        self.assertEqual([item.site.pk for item in result], [kept.pk])
        self.assertNotIn(engine.site_text(dropped), encoder.texts)
        complete.assert_not_called()


    @patch('matching.engine.llm_client.complete')
    def test_two_sites_of_one_institution_are_returned_separately(self, complete):
        institution = Institution.objects.create(
            name='Ключи',
            description='Парк и источники',
            treatment_profile='Общий профиль',
        )
        yalta = self.make_site(
            'Крым, Ялта',
            '3000',
            'ванны',
            institution=institution,
        )
        alushta = self.make_site(
            'Крым, Алушта',
            '4000',
            'грязи',
            institution=institution,
        )
        vectors = {
            _QUERY: [1.0, 0.0],
            engine.site_text(yalta): [1.0, 0.0],
            engine.site_text(alushta): [1.0, 0.0],
        }

        result = engine.match(
            Preferences(budget='8000', region='Крым', query=_QUERY),
            encoder=MappingEncoder(vectors),
        )

        self.assertEqual([item.site.pk for item in result], [yalta.pk, alushta.pk])
        self.assertEqual(
            [item.site.institution_id for item in result],
            [institution.pk, institution.pk],
        )
        complete.assert_not_called()
