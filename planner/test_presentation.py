import csv
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Institution, Site

from .models import Plan
from .presentation import clean_copy, format_price_range, present_route, split_phrases

User = get_user_model()

_CATALOG = Path(__file__).resolve().parent.parent / 'catalog' / 'data' / 'sanatoria.csv'

_BROKEN_PROCEDURES = (
    'Общий анализ крови и мочи, УЗИ органов брюшной полости, ЭКГ, '
    'консультации терапевта, невролога, кардиолога, ортопеда. '
    'Лечебная физкультура, общий массаж, физиотерапевтические процедуры по показаниям.'
)
_BROKEN_EXCURSIONS = (
    'Экскурсия в Государственный музей истории космонавтики имени К.Э. Циолковского, '
    'экскурсия по старому городу (Дом Гостево, Дом Салтыкова-Щедрина), '
    'прогулка по парку культуры и отдыха им. Кирова, посещение планетария.'
)
_ROUTE = (
    'Санаторий «Калуга-курорт»\n'
    'День визита: 1-й день\n'
    'Как добраться: на автобусе от улицы Кирова.\n'
    'Стоимость: от 2900 до 7400 руб. за сутки (данные изprovided источников).\n'
    'Ключевые процедуры: консультации терапевта, невролога и ортопеда.\n'
    'Экскурсии: старый город.\n'
    '\n'
    'Общий бюджетный итог: пребывание обойдётся от 2900 до 7400 руб. за сутки, '
    'лимит 212\u202f312 руб.\n'
    '\n'
    'Советы:\n'
    '1. Забронировать процедуры заранее.\n'
    '2. Рассмотреть возможность 추가ть индивидуальные процедуры.\n'
)


class PhraseSplitTests(TestCase):
    def test_period_between_services_is_two_chips(self):
        chips = split_phrases(_BROKEN_PROCEDURES)

        self.assertIn('Консультация ортопеда', chips)
        self.assertIn('Лечебная физкультура', chips)
        self.assertNotIn('ортопеда. Лечебная физкультура', ' '.join(chips))
        self.assertEqual(
            [chip for chip in chips if 'онсультац' in chip],
            [
                'Консультация терапевта',
                'Консультация невролога',
                'Консультация кардиолога',
                'Консультация ортопеда',
            ],
        )

    def test_slash_consultations_are_separate(self):
        self.assertEqual(
            split_phrases('консультации терапевта / невролога / кардиолога'),
            [
                'Консультация терапевта',
                'Консультация невролога',
                'Консультация кардиолога',
            ],
        )

    def test_comma_inside_parentheses_stays_in_one_chip(self):
        chips = split_phrases(_BROKEN_EXCURSIONS)

        city = [chip for chip in chips if 'старому городу' in chip]
        self.assertEqual(city, [
            'Экскурсия по старому городу (Дом Гостево, Дом Салтыкова-Щедрина)',
        ])
        self.assertTrue(any('К.Э. Циолковского' in chip for chip in chips))
        self.assertTrue(any('им. Кирова' in chip for chip in chips))
        self.assertTrue(all(not chip.endswith('.') for chip in chips))
        self.assertTrue(all(chip[0].isupper() for chip in chips))

    def test_single_consultation_does_not_swallow_the_next_service(self):
        chips = split_phrases(
            'консультация гинеколога-эндокринолога, ЛФК для мышц таза'
        )

        self.assertEqual(chips, [
            'Консультация гинеколога-эндокринолога',
            'ЛФК для мышц таза',
        ])

    def test_semicolon_splits_and_period_in_initials_does_not(self):
        chips = split_phrases('массаж; экскурсия к усадьбе Л.Н. Толстого')

        self.assertEqual(chips, ['Массаж', 'Экскурсия к усадьбе Л.Н. Толстого'])


class PriceFormatTests(TestCase):
    def test_range_has_no_kopecks(self):
        self.assertEqual(format_price_range(Decimal('2900.00'), Decimal('7400.00')), '2 900–7 400 ₽')
        self.assertNotIn(',00', format_price_range(Decimal('2900.00'), Decimal('7400.00')))
        self.assertNotIn('.00', format_price_range(Decimal('2900.00'), Decimal('7400.00')))

    def test_equal_ends_are_one_amount(self):
        self.assertEqual(format_price_range(Decimal('5000.40'), Decimal('5000.40')), '5 000 ₽')


class RouteCopyTests(TestCase):
    def test_garbage_and_foreign_letters_are_removed(self):
        cleaned = clean_copy(_ROUTE)

        self.assertNotIn('provided', cleaned.casefold())
        self.assertNotIn('rovided', clean_copy('заметка изрrovided в тексте').casefold())
        self.assertNotIn('изр', cleaned)
        self.assertNotIn('추가', cleaned)
        self.assertIn('2 900–7 400 ₽', cleaned)
        self.assertIn('212 312 ₽', cleaned)
        self.assertNotIn('руб', cleaned.casefold())
        self.assertIn('возможность добавить индивидуальные', cleaned)

    def test_plain_route_text_stays_intact(self):
        self.assertEqual(clean_copy('День первый\nДень второй'), 'День первый\nДень второй')

    def test_present_route_uses_site_facts(self):
        institution = Institution.objects.create(name='Санаторий «Калуга-курорт»', treatment_profile='Общий')
        site = Site.objects.create(
            institution=institution,
            address='улица Кирова, 112',
            latitude=Decimal('54.520000'),
            longitude=Decimal('36.270000'),
            transport_accessibility=5,
            min_daily_price=Decimal('2900.00'),
            max_daily_price=Decimal('7400.00'),
            procedures=_BROKEN_PROCEDURES,
            excursions=_BROKEN_EXCURSIONS,
        )

        route = present_route(_ROUTE, [site])

        self.assertEqual(route.stops[0].name, 'Санаторий «Калуга-курорт»')
        self.assertEqual(route.stops[0].day, 'День 1')
        self.assertEqual(route.stops[0].price, '2 900–7 400 ₽')
        self.assertEqual(route.stops[0].transport, 5)
        self.assertEqual(route.stops[0].arrival, 'На автобусе от улицы Кирова.')
        self.assertIn('Консультация ортопеда', route.stops[0].procedures)
        self.assertIn('Лечебная физкультура', route.stops[0].procedures)
        self.assertIn(
            'Экскурсия по старому городу (Дом Гостево, Дом Салтыкова-Щедрина)',
            route.stops[0].excursions,
        )
        self.assertIn('2 900–7 400 ₽', route.summary)
        self.assertTrue(any('добавить индивидуальные' in tip for tip in route.tips))
        self.assertNotIn('provided', route.summary.casefold())
        self.assertEqual(route.extra, '')


class CatalogPhraseTests(TestCase):
    def test_catalog_rows_are_one_service_per_chip(self):
        with _CATALOG.open(encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))

        blob = '\n'.join(row['procedures'] + '\n' + row['excursions'] for row in rows)
        self.assertNotIn('Дом Гостево', blob)
        self.assertNotIn('ортопеда. Лечебная', blob)
        for token in ('Premium', 'Detox', 'VIP', 'partisan', 'provided', 'органик'):
            self.assertNotIn(token, blob)

        kaluga = next(
            row for row in rows
            if row['institution_name'] == 'Санаторий «Калуга-курорт»'
            and 'консультация ортопеда' in row['procedures']
        )
        procedures = split_phrases(kaluga['procedures'])
        excursions = split_phrases(kaluga['excursions'])
        self.assertIn('Консультация ортопеда', procedures)
        self.assertIn('Лечебная физкультура', procedures)
        self.assertIn(
            'Экскурсия по старому городу (Дом Гостева, дом Салтыкова-Щедрина)',
            excursions,
        )
        self.assertTrue(any('К.Э. Циолковского' in chip for chip in excursions))
        self.assertTrue(any('им. Кирова' in chip for chip in excursions))


class RouteCardTests(TestCase):
    def test_plan_page_renders_card_and_drops_garbage(self):
        user = User.objects.create_user('guest', password='secret-pass')
        institution = Institution.objects.create(
            name='Санаторий «Калуга-курорт»',
            treatment_profile='Общий',
        )
        site = Site.objects.create(
            institution=institution,
            address='улица Кирова, 112',
            latitude=Decimal('54.520000'),
            longitude=Decimal('36.270000'),
            transport_accessibility=5,
            min_daily_price=Decimal('2900.00'),
            max_daily_price=Decimal('7400.00'),
            procedures=_BROKEN_PROCEDURES,
            excursions=_BROKEN_EXCURSIONS,
        )
        plan = Plan.objects.create(user=user, route_text=_ROUTE)
        plan.sites.add(site)
        self.client.force_login(user)

        response = self.client.get(reverse('plan_detail', args=[plan.pk]))
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'route-card__stripe')
        self.assertContains(response, 'Санаторий «Калуга-курорт»')
        self.assertContains(response, 'День 1')
        self.assertContains(response, '2 900–7 400 ₽')
        self.assertContains(response, 'Транспорт 5 из 5')
        self.assertContains(response, 'Как добраться')
        self.assertContains(response, 'Процедуры')
        self.assertContains(response, 'Экскурсии')
        self.assertContains(response, 'Итог')
        self.assertContains(response, 'Советы')
        self.assertContains(response, 'Консультация ортопеда')
        self.assertContains(response, 'Лечебная физкультура')
        self.assertContains(response, 'Экскурсия по старому городу (Дом Гостево, Дом Салтыкова-Щедрина)')
        self.assertNotContains(response, 'изprovided')
        self.assertNotContains(response, '추가')
        self.assertNotContains(response, '>Дом Салтыкова')
        self.assertLess(html.index('class="price"'), html.index('2 900–7 400 ₽'))

        css = Path(__file__).resolve().parent.parent.joinpath('static', 'style.css').read_text(encoding='utf-8')
        price_rule = css.split('.price {', 1)[1].split('}', 1)[0]
        self.assertIn('white-space: nowrap', price_rule)
        self.assertIn('.route-card__stripe', css)
        self.assertNotIn('overflow-wrap: anywhere', css.split('.tags li {', 1)[1].split('}', 1)[0])
