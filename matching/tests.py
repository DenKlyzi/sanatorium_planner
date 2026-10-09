import hashlib
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from catalog.models import (
    SEASON_SUMMER,
    SEASON_WINTER,
    SEASON_YEAR_ROUND,
    Institution,
    Site,
)
from matching import engine
from matching.engine import Preferences
from matching.models import SiteEmbedding

_QUERY = 'минеральные ванны'


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
    def test_joins_six_fields_in_order(self):
        site = Site(
            address=' Крым, Ялта ',
            procedures='  ванны ',
            excursions='море',
            transport_accessibility=5,
            min_daily_price=Decimal('1000.00'),
            max_daily_price=Decimal('2000.00'),
            institution=Institution(
                name=' Ключи ',
                treatment_profile=' кардиология ',
                description=' Описание ',
            ),
        )

        self.assertEqual(
            engine.site_text(site),
            'Ключи\nкардиология\nОписание\nКрым, Ялта\nванны\nморе',
        )

    def test_skips_blank_parts(self):
        site = Site(
            address='Крым, Ялта',
            procedures='  ',
            excursions='',
            institution=Institution(
                name='Ключи',
                treatment_profile='',
                description='Описание',
            ),
        )

        self.assertEqual(engine.site_text(site), 'Ключи\nОписание\nКрым, Ялта')


class ExplainTests(SimpleTestCase):
    def test_system_prompt_names_shape_and_gaps(self):
        prompt = engine.EXPLAIN_SYSTEM_PROMPT
        folded = prompt.casefold()

        self.assertIn('3–6', prompt)
        self.assertIn('предложен', folded)
        self.assertIn('русск', folded)
        self.assertIn('markdown', folded)
        self.assertIn('цифр', folded)
        self.assertIn('пробелы в совпадении', folded)
        self.assertIn('прямо', folded)

    @patch('matching.engine.llm_client.complete', return_value='подходит')
    def test_user_text_includes_budget_region_query_address_and_prices(self, complete):
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
        self.assertIn('Бюджет: 8000', user)
        self.assertIn('Регион: Крым', user)
        self.assertIn('Запрос: ванны у моря', user)
        self.assertIn('Адрес: Крым, Ялта', user)
        self.assertIn('4500.00', user)
        self.assertIn('9200.00', user)
        self.assertIn('Парк и источники', user)
        self.assertIn('ласточкино гнездо', user)
        self.assertIn('Ключи', user)


class SiteFixtures:
    def setUp(self):
        super().setUp()
        engine._QUERY_VECTORS.clear()

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
        transport=3,
        season=SEASON_YEAR_ROUND,
        limited_mobility_access=False,
        rating=0,
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
            transport_accessibility=transport,
            min_daily_price=Decimal(min_price),
            max_daily_price=Decimal(max_price if max_price is not None else min_price),
            procedures=procedures,
            excursions=excursions,
            season=season,
            limited_mobility_access=limited_mobility_access,
            rating=rating,
        )


class MatchTests(SiteFixtures, TestCase):
    def test_filters_region_and_budget_before_encoding(self):
        included = [
            self.make_site(address, price, procedures)
            for address, price, procedures in (
                ('Крым, Алушта', '1000', 'ванны'),
                ('Крым, Ялта', '2000', 'грязи'),
                ('город Ялта, Крым', '3000', 'бассейн'),
                ('Крым, Евпатория', '4000', 'ингаляции'),
                ('Крым, Саки', '5000', 'лфк'),
                ('Крым, Судак', '6000', 'массаж'),
            )
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
        for site in (*included, exact_budget):
            vectors[engine.site_text(site)] = [1.0, 0.0]
        encoder = MappingEncoder(vectors)

        result = engine.match(
            Preferences(budget='8000', region='кРыМ', query=f'  {_QUERY}  '),
            encoder=encoder,
        )

        returned_ids = {item.site.pk for item in result}
        self.assertNotIn(excluded_region.pk, returned_ids)
        self.assertNotIn(excluded_budget.pk, returned_ids)
        self.assertNotIn(engine.site_text(excluded_region), encoder.texts)
        self.assertNotIn(engine.site_text(excluded_budget), encoder.texts)
        self.assertEqual(encoder.texts[0], _QUERY)
        self.assertIn(engine.site_text(exact_budget), encoder.texts)
        self.assertIn(engine.site_text(included[0]), encoder.texts)
        self.assertLessEqual(returned_ids, {site.pk for site in included} | {exact_budget.pk})

    def test_region_treats_ye_and_yo_as_one_letter(self):
        # «Калужская» и «калужская» должны найти один и тот же адрес.
        # «е» в запросе совпадает с «ё» в адресе и наоборот.
        address = 'посёлок Воробьёво, Малоярославецкий район, Калужская область'
        kaluga = self.make_site(address, '1000', 'ванны')
        yo_only = self.make_site('ПОСЁЛОК Борок, Жуковский район', '1000', 'грязи')
        other = self.make_site('Сочи, центр', '1000', 'море')
        vectors = {_QUERY: [1.0, 0.0]}
        for site in (kaluga, yo_only, other):
            vectors[engine.site_text(site)] = [1.0, 0.0]

        def addresses(region):
            result = engine.match(
                Preferences(budget=5000, region=region, query=_QUERY),
                encoder=MappingEncoder(vectors),
            )
            return [item.site.address for item in result]

        self.assertEqual(addresses('Калужская'), [address])
        self.assertEqual(addresses('калужская'), [address])
        self.assertEqual(addresses('Воробьево'), [address])
        self.assertEqual(addresses('ВОРОБЬЁВО'), [address])
        self.assertEqual(addresses('поселок'), [address, yo_only.address])
        self.assertEqual(addresses('ПОСЁЛОК'), [address, yo_only.address])
        self.assertEqual(addresses('Сочи'), [other.address])

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
        self.assertAlmostEqual(result[0].score, result[1].score)

    def test_blank_region_keeps_every_affordable_site(self):
        crimea = self.make_site('Крым, Ялта', '1000', 'ванны')
        sochi = self.make_site('Сочи, центр', '1000', 'море')
        vectors = {
            _QUERY: [1.0, 0.0],
            engine.site_text(crimea): [1.0, 0.0],
            engine.site_text(sochi): [1.0, 0.0],
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
        self.assertEqual(embed.call_args.args[0], ['ванны', engine.site_text(site)])
        self.assertEqual(result[0].site.pk, site.pk)
        self.assertAlmostEqual(
            result[0].score,
            0.45 * 1 + 0.2 * 1 + 0.1 * 0 + 0.1 * 0.5 + 0.1 * 0.8 + 0.05 * 0.5,
        )

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


class ListEncoder:
    """Векторы по порядку текстов, не по ключу site_text."""

    def __init__(self, vectors):
        self.vectors = vectors
        self.texts = []

    def encode(self, texts):
        self.texts = list(texts)
        return list(self.vectors)


class ScoreFormulaTests(SiteFixtures, TestCase):
    """Итоговый score: шесть слагаемых, порог 0.15, не больше пяти площадок."""

    def match(self, sites, vectors, *, budget='5000', query=_QUERY):
        ordered = sorted(sites, key=lambda site: site.address)
        encoder = ListEncoder([
            [1.0, 0.0],
            *[vectors[site.pk] for site in ordered],
        ])
        return engine.match(
            Preferences(budget=budget, region='Крым', query=query),
            encoder=encoder,
        )

    def test_similarity_moves_score(self):
        better = self.make_site('Крым, Алушта', '5000', 'грязи', excursions='горы', transport=1)
        worse = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', transport=1)
        result = self.match(
            [better, worse],
            {better.pk: [1.0, 0.0], worse.pk: [1.0, 2.0]},
        )

        scores = {item.site.pk: item.score for item in result}
        high = _cosine([1.0, 0.0], [1.0, 0.0])
        low = _cosine([1.0, 0.0], [1.0, 2.0])
        self.assertGreater(scores[better.pk], scores[worse.pk])
        self.assertAlmostEqual(
            scores[better.pk] - scores[worse.pk],
            engine._W_SIMILARITY * (high - low),
        )

    def test_procedures_move_score(self):
        hit = self.make_site('Крым, Алушта', '5000', 'ванны', excursions='горы', transport=1)
        miss = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', transport=1)
        result = self.match([hit, miss], {hit.pk: [1.0, 0.0], miss.pk: [1.0, 0.0]})

        scores = {item.site.pk: item.score for item in result}
        delta = engine._text_overlap(_QUERY, 'ванны') - engine._text_overlap(_QUERY, 'грязи')
        self.assertGreater(scores[hit.pk], scores[miss.pk])
        self.assertAlmostEqual(scores[hit.pk] - scores[miss.pk], engine._W_PROCEDURES * delta)

    def test_excursions_move_score(self):
        hit = self.make_site('Крым, Алушта', '5000', 'грязи', excursions='ванны', transport=1)
        miss = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', transport=1)
        result = self.match([hit, miss], {hit.pk: [1.0, 0.0], miss.pk: [1.0, 0.0]})

        scores = {item.site.pk: item.score for item in result}
        delta = engine._text_overlap(_QUERY, 'ванны') - engine._text_overlap(_QUERY, 'горы')
        self.assertGreater(scores[hit.pk], scores[miss.pk])
        self.assertAlmostEqual(scores[hit.pk] - scores[miss.pk], engine._W_EXCURSIONS * delta)

    def test_empty_query_keeps_text_weights(self):
        # Рейтинг 1 даёт 0, поэтому в сумме остаются текстовые веса.
        site = self.make_site(
            'Крым, Ялта',
            '5000',
            'грязи',
            excursions='горы',
            transport=1,
            rating=1,
        )

        result = self.match([site], {site.pk: [1.0, 0.0]}, query='')

        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(
            result[0].score,
            engine._W_SIMILARITY + engine._W_PROCEDURES + engine._W_EXCURSIONS,
        )

    def test_transport_moves_score(self):
        closer = self.make_site('Крым, Алушта', '5000', 'грязи', excursions='горы', transport=5)
        farther = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', transport=1)
        result = self.match(
            [closer, farther],
            {closer.pk: [1.0, 0.0], farther.pk: [1.0, 0.0]},
        )

        scores = {item.site.pk: item.score for item in result}
        delta = engine._transport_score(5) - engine._transport_score(1)
        self.assertGreater(scores[closer.pk], scores[farther.pk])
        self.assertAlmostEqual(scores[closer.pk] - scores[farther.pk], engine._W_TRANSPORT * delta)

    def test_price_moves_score(self):
        cheaper = self.make_site('Крым, Алушта', '0', 'грязи', excursions='горы', transport=1)
        dearer = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', transport=1)
        result = self.match(
            [cheaper, dearer],
            {cheaper.pk: [1.0, 0.0], dearer.pk: [1.0, 0.0]},
        )

        scores = {item.site.pk: item.score for item in result}
        budget = Decimal('5000')
        delta = engine._price_score(Decimal('0'), budget) - engine._price_score(budget, budget)
        self.assertGreater(scores[cheaper.pk], scores[dearer.pk])
        self.assertAlmostEqual(scores[cheaper.pk] - scores[dearer.pk], engine._W_PRICE * delta)

    def test_rating_moves_score(self):
        higher = self.make_site(
            'Крым, Алушта',
            '5000',
            'грязи',
            excursions='горы',
            transport=1,
            rating=5,
        )
        lower = self.make_site(
            'Крым, Ялта',
            '5000',
            'грязи',
            excursions='горы',
            transport=1,
            rating=1,
        )
        result = self.match(
            [higher, lower],
            {higher.pk: [1.0, 0.0], lower.pk: [1.0, 0.0]},
        )

        scores = {item.site.pk: item.score for item in result}
        delta = engine._rating_score(5) - engine._rating_score(1)
        self.assertGreater(scores[higher.pk], scores[lower.pk])
        self.assertAlmostEqual(scores[higher.pk] - scores[lower.pk], engine._W_RATING * delta)

    def test_unspecified_rating_ties_with_middle(self):
        unknown = self.make_site('Крым, Алушта', '5000', 'грязи', excursions='горы', rating=0)
        middle = self.make_site('Крым, Ялта', '5000', 'грязи', excursions='горы', rating=3)
        result = self.match(
            [unknown, middle],
            {unknown.pk: [1.0, 0.0], middle.pk: [1.0, 0.0]},
        )

        self.assertEqual([item.site.pk for item in result], [unknown.pk, middle.pk])
        self.assertAlmostEqual(result[0].score, result[1].score)

    def test_threshold_drops_weak_score_and_keeps_boundary(self):
        # Косинус 0. Транспорт 5 и цена вдвое ниже бюджета дают ровно порог 0.15.
        # Рейтинг 1 даёт 0 и порог не сдвигает. Транспорт 4 при той же цене ниже.
        kept = self.make_site(
            'Крым, Ялта',
            '2500',
            'грязи',
            excursions='горы',
            transport=5,
            rating=1,
        )
        dropped = self.make_site(
            'Крым, Алушта',
            '2500',
            'грязи',
            excursions='горы',
            transport=4,
            rating=1,
        )
        orthogonal = [0.0, 1.0]

        result = self.match(
            [kept, dropped],
            {kept.pk: orthogonal, dropped.pk: orthogonal},
        )

        self.assertEqual([item.site.pk for item in result], [kept.pk])
        self.assertGreaterEqual(result[0].score, 0.15)

    def test_returns_at_most_five_sites(self):
        sites = [
            self.make_site(f'Крым, площадка {index}', '1000', 'грязи', excursions='горы')
            for index in range(6)
        ]
        vector = [1.0, 0.0]
        result = self.match(sites, {site.pk: vector for site in sites})

        self.assertEqual(len(result), 5)
        self.assertEqual(
            [item.site.pk for item in result],
            sorted(site.pk for site in sites)[:5],
        )

    def test_equal_scores_keep_smaller_pk_first(self):
        first = self.make_site('Крым, Ялта', '1000', 'грязи', excursions='горы')
        second = self.make_site('Крым, Алушта', '1000', 'грязи', excursions='горы')
        vector = [1.0, 0.0]
        result = self.match([first, second], {first.pk: vector, second.pk: vector})

        self.assertEqual([item.site.pk for item in result], [first.pk, second.pk])
        self.assertAlmostEqual(result[0].score, result[1].score)


class EmbeddingCacheTests(SiteFixtures, TestCase):
    def prefs(self, query='ванны'):
        return Preferences(budget=5000, region='Крым', query=query)

    @patch('matching.engine.llm_client.embed')
    def test_repeat_match_skips_embed_and_stores_vector(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        embed.return_value = [[1.0, 0.0], [0.0, 1.0]]

        first = engine.match(self.prefs())
        embed.assert_called_once_with(['ванны', engine.site_text(site)])

        row = SiteEmbedding.objects.get()
        self.assertEqual(row.site_id, site.pk)
        self.assertEqual(row.model_name, settings.EMBEDDING_MODEL_NAME.strip())
        self.assertEqual(row.vector, [0.0, 1.0])
        self.assertEqual(
            row.text_hash,
            hashlib.sha256(engine.site_text(site).encode('utf-8')).hexdigest(),
        )

        embed.reset_mock()
        embed.return_value = [[9.0, 9.0]]
        again = engine.match(self.prefs())

        embed.assert_not_called()
        self.assertEqual(again[0].site.pk, site.pk)
        self.assertAlmostEqual(again[0].score, first[0].score)

    @patch('matching.engine.llm_client.embed')
    def test_changed_site_text_embeds_only_that_site(self, embed):
        kept = self.make_site('Крым, Ялта', '1000', 'ванны')
        changed = self.make_site('Крым, Алушта', '1000', 'грязи')
        embed.return_value = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        engine.match(self.prefs())

        changed.procedures = 'массаж'
        changed.save()
        embed.reset_mock()
        embed.return_value = [[0.0, 1.0]]
        engine.match(self.prefs())

        embed.assert_called_once_with([engine.site_text(changed)])
        self.assertEqual(SiteEmbedding.objects.filter(site=kept).count(), 1)
        self.assertEqual(SiteEmbedding.objects.filter(site=changed).count(), 2)

    @patch('matching.engine.llm_client.embed')
    def test_reverted_text_uses_the_old_row(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        embed.return_value = [[1.0, 0.0], [0.0, 1.0]]
        engine.match(self.prefs())

        site.procedures = 'грязи'
        site.save()
        embed.return_value = [[1.0, 0.0]]
        engine.match(self.prefs())

        site.procedures = 'ванны'
        site.save()
        embed.reset_mock()
        engine.match(self.prefs())

        embed.assert_not_called()
        self.assertEqual(SiteEmbedding.objects.filter(site=site).count(), 2)

    @patch('matching.engine.llm_client.embed')
    def test_new_query_embeds_only_the_query(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        embed.return_value = [[1.0, 0.0], [1.0, 0.0]]
        engine.match(self.prefs())

        embed.reset_mock()
        embed.return_value = [[1.0, 0.0]]
        result = engine.match(self.prefs('минеральные ванны'))

        embed.assert_called_once_with(['минеральные ванны'])
        self.assertEqual(result[0].site.pk, site.pk)
        self.assertEqual(SiteEmbedding.objects.count(), 1)

    @patch('matching.engine.llm_client.embed')
    def test_price_change_reuses_vector(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        embed.return_value = [[1.0, 0.0], [1.0, 0.0]]
        first = engine.match(self.prefs())

        site.min_daily_price = Decimal('4000')
        site.max_daily_price = Decimal('5000')
        site.save()
        embed.reset_mock()
        second = engine.match(self.prefs())

        embed.assert_not_called()
        self.assertEqual(second[0].site.pk, site.pk)
        self.assertLess(second[0].score, first[0].score)

    @patch('matching.engine.llm_client.embed')
    def test_rating_change_reuses_vector_and_moves_score(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны', rating=1)
        embed.return_value = [[1.0, 0.0], [1.0, 0.0]]
        first = engine.match(self.prefs())

        site.rating = 5
        site.save()
        embed.reset_mock()
        second = engine.match(self.prefs())

        embed.assert_not_called()
        self.assertEqual(second[0].site.pk, site.pk)
        self.assertAlmostEqual(
            second[0].score - first[0].score,
            engine._W_RATING * (engine._rating_score(5) - engine._rating_score(1)),
        )

    @patch('matching.engine.llm_client.embed')
    def test_institution_text_change_embeds_again(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны', description='Парк')
        embed.return_value = [[1.0, 0.0], [0.0, 1.0]]
        engine.match(self.prefs())

        site.institution.description = 'Новое описание'
        site.institution.save()
        embed.reset_mock()
        embed.return_value = [[0.0, 1.0]]
        engine.match(self.prefs())

        embed.assert_called_once_with([engine.site_text(site)])

    @patch('matching.engine.llm_client.embed')
    def test_other_model_name_embeds_again(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        embed.return_value = [[1.0, 0.0], [0.0, 1.0]]
        with self.settings(EMBEDDING_MODEL_NAME='model-a'):
            engine.match(self.prefs())

        embed.reset_mock()
        embed.return_value = [[1.0, 0.0], [0.0, 1.0]]
        with self.settings(EMBEDDING_MODEL_NAME='model-b'):
            engine.match(self.prefs())

        embed.assert_called_once_with(['ванны', engine.site_text(site)])
        self.assertEqual(
            set(SiteEmbedding.objects.values_list('model_name', flat=True)),
            {'model-a', 'model-b'},
        )

    @patch('matching.engine.llm_client.embed')
    def test_custom_encoder_does_not_fill_cache(self, embed):
        site = self.make_site('Крым, Ялта', '1000', 'ванны')
        vectors = {
            'ванны': [1.0, 0.0],
            engine.site_text(site): [1.0, 0.0],
        }

        result = engine.match(self.prefs(), encoder=MappingEncoder(vectors))

        self.assertEqual(result[0].site.pk, site.pk)
        self.assertEqual(SiteEmbedding.objects.count(), 0)
        self.assertEqual(engine._QUERY_VECTORS, {})
        embed.assert_not_called()


class OptionalConstraintTests(SiteFixtures, TestCase):
    """Сезон и маломобильность режут выдачу только как явные аргументы match."""

    def preferences(self):
        return Preferences(budget=5000, region='Крым', query=_QUERY)

    def encoder(self, *sites):
        vectors = {_QUERY: [1.0, 0.0]}
        for site in sites:
            vectors[engine.site_text(site)] = [1.0, 0.0]
        return MappingEncoder(vectors)

    def test_omitted_arguments_do_not_cut_results(self):
        summer = self.make_site(
            'Крым, лето',
            '1000',
            'грязи',
            season=SEASON_SUMMER,
            limited_mobility_access=False,
        )
        winter = self.make_site(
            'Крым, зима',
            '1000',
            'грязи',
            season=SEASON_WINTER,
            limited_mobility_access=True,
        )

        omitted = engine.match(self.preferences(), encoder=self.encoder(summer, winter))
        explicit_none = engine.match(
            self.preferences(),
            encoder=self.encoder(summer, winter),
            season=None,
            limited_mobility_access=None,
        )

        expected = {summer.pk, winter.pk}
        self.assertEqual({item.site.pk for item in omitted}, expected)
        self.assertEqual({item.site.pk for item in explicit_none}, expected)

    def test_season_keeps_that_season_and_year_round(self):
        summer = self.make_site(
            'Крым, лето',
            '1000',
            'грязи',
            season=SEASON_SUMMER,
            limited_mobility_access=False,
        )
        year_round = self.make_site(
            'Крым, круглый',
            '1000',
            'грязи',
            season=SEASON_YEAR_ROUND,
            limited_mobility_access=True,
        )
        winter = self.make_site(
            'Крым, зима',
            '1000',
            'грязи',
            season=SEASON_WINTER,
            limited_mobility_access=True,
        )
        expensive = self.make_site(
            'Крым, дорого',
            '9000',
            'грязи',
            season=SEASON_SUMMER,
            max_price='12000',
        )
        other_region = self.make_site('Сочи, лето', '1000', 'грязи', season=SEASON_SUMMER)
        sites = (summer, year_round, winter, expensive, other_region)

        for season in (' лето ', 'SUMMER'):
            encoder = self.encoder(*sites)
            result = engine.match(self.preferences(), encoder=encoder, season=season)

            self.assertEqual({item.site.pk for item in result}, {summer.pk, year_round.pk})
            self.assertIn(engine.site_text(summer), encoder.texts)
            self.assertIn(engine.site_text(year_round), encoder.texts)
            self.assertNotIn(engine.site_text(winter), encoder.texts)
            self.assertNotIn(engine.site_text(expensive), encoder.texts)
            self.assertNotIn(engine.site_text(other_region), encoder.texts)

    def test_year_round_request_drops_seasonal_sites(self):
        year_round = self.make_site('Крым, круглый', '1000', 'грязи', season=SEASON_YEAR_ROUND)
        summer = self.make_site('Крым, лето', '1000', 'грязи', season=SEASON_SUMMER)
        encoder = self.encoder(year_round, summer)

        result = engine.match(self.preferences(), encoder=encoder, season='круглый год')

        self.assertEqual([item.site.pk for item in result], [year_round.pk])
        self.assertNotIn(engine.site_text(summer), encoder.texts)

    def test_mobility_filters_only_the_passed_flag(self):
        accessible = self.make_site(
            'Крым, зима',
            '1000',
            'грязи',
            season=SEASON_WINTER,
            limited_mobility_access=True,
        )
        inaccessible = self.make_site(
            'Крым, лето',
            '1000',
            'грязи',
            season=SEASON_SUMMER,
            limited_mobility_access=False,
        )

        only_accessible = engine.match(
            self.preferences(),
            encoder=self.encoder(accessible, inaccessible),
            limited_mobility_access=True,
        )
        only_inaccessible = engine.match(
            self.preferences(),
            encoder=self.encoder(accessible, inaccessible),
            limited_mobility_access=False,
        )

        self.assertEqual([item.site.pk for item in only_accessible], [accessible.pk])
        self.assertEqual([item.site.pk for item in only_inaccessible], [inaccessible.pk])

    def test_both_constraints_apply_together(self):
        kept = self.make_site(
            'Крым, лето доступно',
            '1000',
            'грязи',
            season=SEASON_SUMMER,
            limited_mobility_access=True,
        )
        year_round = self.make_site(
            'Крым, круглый доступно',
            '1000',
            'грязи',
            season=SEASON_YEAR_ROUND,
            limited_mobility_access=True,
        )
        wrong_season = self.make_site(
            'Крым, зима доступно',
            '1000',
            'грязи',
            season=SEASON_WINTER,
            limited_mobility_access=True,
        )
        wrong_access = self.make_site(
            'Крым, лето без доступа',
            '1000',
            'грязи',
            season=SEASON_SUMMER,
            limited_mobility_access=False,
        )
        encoder = self.encoder(kept, year_round, wrong_season, wrong_access)

        result = engine.match(
            self.preferences(),
            encoder=encoder,
            season=SEASON_SUMMER,
            limited_mobility_access=True,
        )

        self.assertEqual({item.site.pk for item in result}, {kept.pk, year_round.pk})
        self.assertNotIn(engine.site_text(wrong_season), encoder.texts)
        self.assertNotIn(engine.site_text(wrong_access), encoder.texts)

    def test_invalid_constraint_raises_before_encoding(self):
        self.make_site('Крым, Ялта', '1000', 'грязи', season=SEASON_SUMMER)

        class Boom:
            def encode(self, texts):
                raise AssertionError(texts)

        for season in ('never', '   '):
            with self.assertRaises(ValueError):
                engine.match(self.preferences(), season=season, encoder=Boom())
        with self.assertRaises(TypeError):
            engine.match(self.preferences(), season=1, encoder=Boom())
        for flag in (1, 0, 'да'):
            with self.assertRaises(TypeError):
                engine.match(
                    self.preferences(),
                    limited_mobility_access=flag,
                    encoder=Boom(),
                )

    def test_invalid_season_raises_on_empty_catalog(self):
        with self.assertRaises(ValueError):
            engine.match(self.preferences(), season='never')


class RatingScoreTests(SimpleTestCase):
    def test_unspecified_is_neutral_and_scale_is_one_to_five(self):
        self.assertEqual(engine._rating_score(None), 0.5)
        self.assertEqual(engine._rating_score(0), 0.5)
        self.assertEqual(engine._rating_score(''), 0.5)
        self.assertEqual(engine._rating_score(True), 0.5)
        self.assertEqual(engine._rating_score(1), 0.0)
        self.assertEqual(engine._rating_score(3), 0.5)
        self.assertEqual(engine._rating_score('5'), 1.0)
        self.assertEqual(engine._rating_score(6), 1.0)


class TextOverlapTests(SimpleTestCase):
    def test_empty_query_does_not_penalize(self):
        self.assertEqual(engine._text_overlap('', 'ванны'), 1.0)
        self.assertEqual(engine._text_overlap('   ', 'ванны'), 1.0)
        self.assertEqual(engine._text_overlap('и в на', 'ванны'), 1.0)
        self.assertEqual(engine._text_overlap('', ''), 1.0)
        self.assertEqual(engine._text_overlap('', None), 1.0)

    def test_overlap_is_share_of_query_words(self):
        self.assertEqual(engine._text_overlap('минеральные ванны', 'ванны'), 0.5)
        self.assertEqual(engine._text_overlap('минеральные ванны', 'грязи'), 0.0)
        self.assertEqual(engine._text_overlap('Минеральные Ванны', 'ВАННЫ'), 0.5)
        self.assertEqual(engine._text_overlap('ванны ванны', 'ванны'), 1.0)
        self.assertEqual(engine._text_overlap('минеральные ванны', ''), 0.0)
        self.assertEqual(engine._text_overlap('минеральные ванны', None), 0.0)

    def test_match_is_whole_word_not_substring(self):
        self.assertEqual(engine._text_overlap('ван', 'ванны'), 0.0)
        self.assertEqual(engine._text_overlap('для, массаж', 'массаж.'), 1.0)
        self.assertEqual(engine._text_overlap('ванны, массаж', 'массаж.'), 0.5)

    def test_weights_stay_explicit_and_sum_to_one(self):
        weights = (
            engine._W_SIMILARITY,
            engine._W_PROCEDURES,
            engine._W_EXCURSIONS,
            engine._W_TRANSPORT,
            engine._W_PRICE,
            engine._W_RATING,
        )
        self.assertEqual(weights, (0.45, 0.2, 0.1, 0.1, 0.1, 0.05))
        self.assertEqual(sum(weights), 1)
        self.assertLess(engine._W_RATING, engine._W_PRICE)
