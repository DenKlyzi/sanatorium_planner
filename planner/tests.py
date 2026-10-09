from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Institution, Site
from matching.engine import ScoredSite

from . import views
from .models import Plan

User = get_user_model()

_RESULT_TEMPLATE = Path(__file__).resolve().parent / 'templates' / 'planner' / 'result.html'
_PLAN_PROMPT_TODO = '# TODO(team): текст системного промпта маршрута пишем сами.'
_MAP_TODO = 'TODO(team): блок карты Leaflet'
_CARD_TODO = 'TODO(team): финальные тексты карточек'
_PAID_MAP_MARKERS = (
    'maps.googleapis',
    'mapbox',
    'maptiler',
    'apikey',
    'api_key',
    'api-key',
    'leaflet.js',
    'leaflet.css',
    'openstreetmap',
    'yandex.ru/maps',
)


class PlanPromptTests(TestCase):
    def test_route_system_prompt_is_left_for_the_team(self):
        source = Path(views.__file__).read_text(encoding='utf-8')

        self.assertLess(source.index(_PLAN_PROMPT_TODO), source.index('llm_client.complete('))
        self.assertEqual(views.PLAN_SYSTEM_PROMPT, '')


class ResultTemplateTests(TestCase):
    def test_map_and_card_copy_are_left_for_the_team(self):
        source = _RESULT_TEMPLATE.read_text(encoding='utf-8')

        map_todo = source.index(_MAP_TODO)
        map_block = source.index('id="leaflet-map"')
        card_todo = source.index(_CARD_TODO)
        card_name = source.index('card.site.institution.name')

        self.assertLess(map_todo, map_block)
        self.assertLess(card_todo, card_name)
        folded = source.casefold()
        for marker in _PAID_MAP_MARKERS:
            self.assertNotIn(marker, folded)


class SearchViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('guest', password='secret-pass')
        self.client.force_login(self.user)

    def make_site(self, name='Ключи', address='Крым, Ялта'):
        institution = Institution.objects.create(
            name=name,
            description='Парк и источники',
            treatment_profile='Общий',
        )
        return Site.objects.create(
            institution=institution,
            address=address,
            latitude=Decimal('44.500000'),
            longitude=Decimal('34.160000'),
            transport_accessibility=4,
            min_daily_price=Decimal('4500.00'),
            max_daily_price=Decimal('9200.00'),
            procedures='ванны',
            excursions='ласточкино гнездо',
        )

    def test_anonymous_search_redirects_to_login(self):
        self.client.logout()

        response = self.client.get(reverse('search'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_login_page_uses_bootstrap(self):
        self.client.logout()

        response = self.client.get(reverse('login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'bootstrap.min.css')
        self.assertContains(response, 'form-control')

    def test_get_shows_bootstrap_form(self):
        response = self.client.get(reverse('search'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'bootstrap.min.css')
        self.assertContains(response, 'Бюджет за сутки')
        self.assertContains(response, 'Регион')
        self.assertContains(response, 'Свободный текст предпочтений')
        self.assertContains(response, 'Желаемые процедуры')
        self.assertContains(response, 'Желаемые экскурсии')

    def test_invalid_budget_does_not_call_engine(self):
        with patch('planner.views.match') as match:
            response = self.client.post(reverse('search'), {'budget': '-5', 'region': 'Крым'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors['budget'])
        match.assert_not_called()

    @patch('planner.views.explain', return_value='<script>alert(1)</script>')
    @patch('planner.views.match')
    def test_post_shows_site_cards(self, match, explain):
        site = self.make_site()
        match.return_value = [ScoredSite(site=site, score=0.5)]

        response = self.client.post(
            reverse('search'),
            {
                'budget': '8000',
                'region': '  Крым ',
                'query': ' море ',
                'procedures': 'ванны',
                'excursions': 'дворец',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'planner/result.html')
        preferences = match.call_args.args[0]
        self.assertEqual(preferences.budget, Decimal('8000.00'))
        self.assertEqual(preferences.region, 'Крым')
        self.assertEqual(preferences.query, 'море\nПроцедуры: ванны\nЭкскурсии: дворец')
        explain.assert_called_once_with(preferences, site)
        self.assertContains(response, 'Ключи')
        self.assertContains(response, 'Цена за сутки: <span class="price">4 500–9 200 ₽</span>')
        self.assertContains(response, 'Транспортная доступность: 4 из 5')
        body = response.content.decode()
        self.assertEqual(body.count('id="id_budget"'), 1)
        self.assertIn('id="plan_budget"', body)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'собрать план')
        self.assertContains(response, 'checked')

    @patch('planner.views.explain')
    @patch('planner.views.match')
    def test_explain_failure_stops_and_keeps_cards(self, match, explain):
        first = self.make_site(name='Ключи')
        second = self.make_site(name='Маяк', address='Крым, Алушта')
        match.return_value = [
            ScoredSite(site=first, score=0.4),
            ScoredSite(site=second, score=0.2),
        ]
        explain.side_effect = ValueError('LLM_API_KEY не задан')

        response = self.client.post(
            reverse('search'),
            {'budget': '8000', 'region': 'Крым', 'query': 'море'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(explain.call_count, 1)
        self.assertContains(response, 'Ключи')
        self.assertContains(response, 'Маяк')
        self.assertContains(response, 'Не удалось получить объяснение.')

    @patch('planner.views.match')
    def test_match_failure_does_not_explain(self, match):
        match.side_effect = ValueError('LLM_BASE_URL не задан')

        with patch('planner.views.explain') as explain:
            response = self.client.post(
                reverse('search'),
                {'budget': '8000', 'region': 'Крым', 'query': 'море'},
            )

        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'Не удалось подобрать площадки.', status_code=503)
        explain.assert_not_called()

    @patch('planner.views.match', return_value=[])
    def test_empty_match_has_no_plan_button(self, match):
        response = self.client.post(
            reverse('search'),
            {'budget': '1000', 'region': 'Антарктида', 'query': 'море'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Подходящих площадок нет.')
        self.assertNotContains(response, 'собрать план')
        match.assert_called_once()

    @patch('matching.engine.llm_client.complete', return_value='пояснение')
    def test_empty_catalog_view_does_not_crash(self, complete):
        self.assertEqual(Site.objects.count(), 0)

        response = self.client.post(
            reverse('search'),
            {'budget': '8000', 'region': 'Крым', 'query': 'море'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'planner/result.html')
        self.assertContains(response, 'Подходящих площадок нет.')
        complete.assert_not_called()


class BuildPlanViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('guest', password='secret-pass')
        self.client.force_login(self.user)

    def make_site(self, name, address):
        institution = Institution.objects.create(
            name=name,
            description='Описание',
            treatment_profile='Общий',
        )
        return Site.objects.create(
            institution=institution,
            address=address,
            latitude=Decimal('44.500000'),
            longitude=Decimal('34.160000'),
            transport_accessibility=3,
            min_daily_price=Decimal('3000.00'),
            max_daily_price=Decimal('6000.00'),
            procedures='грязи',
            excursions='набережная',
        )

    def plan_data(self, *sites):
        return {
            'budget': '8000.00',
            'region': 'Крым',
            'query': 'море',
            'procedures': 'ванны',
            'excursions': 'дворец',
            'sites': [site.pk for site in sites],
        }

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse('build_plan'))

        self.assertEqual(response.status_code, 405)

    def test_anonymous_post_does_not_save(self):
        site = self.make_site('Ключи', 'Крым, Ялта')
        self.client.logout()

        response = self.client.post(reverse('build_plan'), self.plan_data(site))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])
        self.assertEqual(Plan.objects.count(), 0)

    def test_missing_sites_does_not_call_llm(self):
        with patch('planner.views.llm_client.complete') as complete:
            response = self.client.post(reverse('build_plan'), self.plan_data())

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Выберите хотя бы одну площадку.', status_code=400)
        complete.assert_not_called()
        self.assertEqual(Plan.objects.count(), 0)

    @patch('planner.views.llm_client.complete', return_value='  День первый\nДень второй  ')
    def test_saves_plan_for_user_and_selected_sites(self, complete):
        later = self.make_site('Маяк', 'Крым, Алушта')
        earlier = self.make_site('Ключи', 'Крым, Ялта')

        response = self.client.post(reverse('build_plan'), self.plan_data(later, earlier), follow=True)

        plan = Plan.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(plan.user, self.user)
        self.assertEqual(
            set(plan.sites.values_list('pk', flat=True)),
            {later.pk, earlier.pk},
        )
        self.assertEqual(plan.route_text, 'День первый\nДень второй')
        self.assertContains(response, 'guest')
        self.assertContains(response, 'День первый')
        body = response.content.decode()
        self.assertLess(body.index('Маяк'), body.index('Ключи'))
        system, user_text = complete.call_args.args
        self.assertEqual(system, views.PLAN_SYSTEM_PROMPT)
        self.assertLess(user_text.index('Маяк'), user_text.index('Ключи'))
        self.assertIn('8000.00', user_text)
        self.assertIn('ванны', user_text)
        self.assertIn('дворец', user_text)
        self.assertNotContains(response, '<i>')

    @patch('planner.views.llm_client.complete', return_value='<i>маршрут</i>')
    def test_route_text_is_escaped(self, complete):
        site = self.make_site('Ключи', 'Крым, Ялта')

        response = self.client.post(reverse('build_plan'), self.plan_data(site), follow=True)

        self.assertContains(response, '&lt;i&gt;маршрут&lt;/i&gt;')
        self.assertNotContains(response, '<i>маршрут</i>')
        complete.assert_called_once()

    @patch('planner.views.llm_client.complete', side_effect=ValueError('LLM_API_KEY не задан'))
    def test_llm_error_does_not_save(self, complete):
        site = self.make_site('Ключи', 'Крым, Ялта')

        response = self.client.post(reverse('build_plan'), self.plan_data(site))

        self.assertEqual(response.status_code, 502)
        self.assertContains(response, 'Не удалось собрать маршрут.', status_code=502)
        self.assertEqual(Plan.objects.count(), 0)
        complete.assert_called_once()

    @patch('planner.views.llm_client.complete', return_value='   ')
    def test_blank_route_does_not_save(self, complete):
        site = self.make_site('Ключи', 'Крым, Ялта')

        response = self.client.post(reverse('build_plan'), self.plan_data(site))

        self.assertEqual(response.status_code, 502)
        self.assertContains(response, 'Модель вернула пустой маршрут.', status_code=502)
        self.assertEqual(Plan.objects.count(), 0)
        complete.assert_called_once()
