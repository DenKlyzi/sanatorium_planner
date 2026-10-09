import csv
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from catalog.admin import InstitutionAdmin, SiteAdmin, SiteInline
from catalog.models import (
    SEASON_SUMMER,
    SEASON_WINTER,
    SEASON_YEAR_ROUND,
    Institution,
    Site,
)

User = get_user_model()

FULL_HEADER = (
    'institution_name',
    'description',
    'treatment_profile',
    'address',
    'latitude',
    'longitude',
    'transport_accessibility',
    'min_daily_price',
    'max_daily_price',
    'procedures',
    'excursions',
    'season',
    'limited_mobility_access',
    'rating',
)
OPTIONAL_HEADER = ('season', 'limited_mobility_access', 'rating')
PROJECT_CSV = settings.BASE_DIR / 'catalog' / 'data' / 'sanatoria.csv'


def render_csv(rows, header=FULL_HEADER):
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(header),
        lineterminator='\n',
        extrasaction='ignore',
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def row(**overrides):
    data = {
        'institution_name': 'Демо-санаторий «Ключи»',
        'description': 'Описание',
        'treatment_profile': 'Общий',
        'address': 'Крым, Ялта',
        'latitude': '44.500000',
        'longitude': '34.160000',
        'transport_accessibility': '4',
        'min_daily_price': '4500.00',
        'max_daily_price': '9200.00',
        'procedures': 'ванны',
        'excursions': 'море',
        'season': 'лето',
        'limited_mobility_access': '1',
        'rating': '4',
    }
    data.update(overrides)
    return data


def make_institution(name='Ключи', **overrides):
    data = {
        'name': name,
        'description': 'Парк и источники',
        'treatment_profile': 'Общий',
    }
    data.update(overrides)
    return Institution.objects.create(**data)


def make_site(institution=None, **overrides):
    if institution is None:
        institution = make_institution()
    data = {
        'institution': institution,
        'address': 'Крым, Ялта',
        'latitude': Decimal('44.500000'),
        'longitude': Decimal('34.160000'),
        'transport_accessibility': 4,
        'min_daily_price': Decimal('4500.00'),
        'max_daily_price': Decimal('9200.00'),
        'procedures': 'ванны',
        'excursions': 'ласточкино гнездо',
    }
    data.update(overrides)
    return Site.objects.create(**data)


class SiteFieldTests(TestCase):
    def test_create_without_new_fields_uses_defaults(self):
        institution = Institution.objects.create(
            name='Ключи',
            description='Парк и источники',
            treatment_profile='Общий',
        )
        site = Site.objects.create(
            institution=institution,
            address='Крым, Ялта',
            latitude=Decimal('44.500000'),
            longitude=Decimal('34.160000'),
            transport_accessibility=4,
            min_daily_price=Decimal('4500.00'),
            max_daily_price=Decimal('9200.00'),
            procedures='ванны',
            excursions='ласточкино гнездо',
        )

        site.refresh_from_db()
        self.assertEqual(site.season, SEASON_YEAR_ROUND)
        self.assertFalse(site.limited_mobility_access)
        self.assertEqual(site.rating, 0)
        site.full_clean()

    def test_institution_name_is_unique(self):
        make_institution('Ключи')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_institution('Ключи', description='Другое описание')

    def test_institution_and_address_pair_is_unique(self):
        institution = make_institution()
        make_site(institution, address='Крым, Ялта')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_site(institution, address='Крым, Ялта', procedures='грязи')

    def test_same_address_is_allowed_for_another_institution(self):
        make_site(make_institution('Ключи'), address='Общий берег')
        make_site(make_institution('Маяк'), address='Общий берег')

        self.assertEqual(Site.objects.filter(address='Общий берег').count(), 2)

    def test_rating_outside_zero_to_five_is_rejected(self):
        site = make_site()
        site.rating = 6

        with self.assertRaises(ValidationError):
            site.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                site.save()

    def test_unknown_season_is_rejected(self):
        site = make_site()
        site.season = 'never'

        with self.assertRaises(ValidationError):
            site.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                site.save()


class LoadDemoTests(TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.directory = Path(self._temp.name)

    def load(self, rows, header=FULL_HEADER):
        path = self.directory / 'rows.csv'
        path.write_text(render_csv(rows, header), encoding='utf-8')
        output = StringIO()
        call_command('load_demo', str(path), stdout=output)
        return output.getvalue()

    def test_creates_institutions_and_sites(self):
        message = self.load(
            [
                row(
                    institution_name='Ключи',
                    address='Крым, Ялта',
                    season=' Лето ',
                    limited_mobility_access='да',
                    rating='4',
                ),
                row(
                    institution_name='Ключи',
                    address='Крым, Алушта',
                    season='круглый год',
                    limited_mobility_access='0',
                    rating='5',
                    min_daily_price='1000.00',
                    max_daily_price='2000.00',
                ),
                row(
                    institution_name='Маяк',
                    address='Сочи',
                    season='зима',
                    limited_mobility_access='нет',
                    rating='2',
                ),
            ]
        )

        self.assertIn('Учреждения: создано 2, обновлено 0.', message)
        self.assertIn('Площадки: создано 3, обновлено 0.', message)
        yalta = Site.objects.get(address='Крым, Ялта')
        alushta = Site.objects.get(address='Крым, Алушта')
        sochi = Site.objects.get(address='Сочи')
        self.assertEqual(yalta.institution.name, 'Ключи')
        self.assertEqual(alushta.institution_id, yalta.institution_id)
        self.assertEqual(yalta.season, SEASON_SUMMER)
        self.assertTrue(yalta.limited_mobility_access)
        self.assertEqual(yalta.rating, 4)
        self.assertEqual(alushta.season, SEASON_YEAR_ROUND)
        self.assertFalse(alushta.limited_mobility_access)
        self.assertEqual(alushta.rating, 5)
        self.assertEqual(sochi.institution.name, 'Маяк')
        self.assertEqual(sochi.season, SEASON_WINTER)
        self.assertFalse(sochi.limited_mobility_access)
        self.assertEqual(sochi.rating, 2)

    def test_updates_existing_institution_and_site(self):
        institution = make_institution(description='было', treatment_profile='старый')
        site = make_site(institution, procedures='старые процедуры')

        message = self.load(
            [
                row(
                    institution_name='Ключи',
                    description='новое описание',
                    treatment_profile='новый профиль',
                    address='Крым, Ялта',
                    latitude='55.750000',
                    longitude='37.620000',
                    transport_accessibility='2',
                    min_daily_price='1000.00',
                    max_daily_price='3000.00',
                    procedures='грязи',
                    excursions='горы',
                    season='зима',
                    limited_mobility_access='1',
                    rating='5',
                )
            ]
        )

        self.assertIn('Учреждения: создано 0, обновлено 1.', message)
        self.assertIn('Площадки: создано 0, обновлено 1.', message)
        institution.refresh_from_db()
        site.refresh_from_db()
        self.assertEqual(Institution.objects.count(), 1)
        self.assertEqual(Site.objects.count(), 1)
        self.assertEqual(institution.description, 'новое описание')
        self.assertEqual(institution.treatment_profile, 'новый профиль')
        self.assertEqual(site.procedures, 'грязи')
        self.assertEqual(site.excursions, 'горы')
        self.assertEqual(site.transport_accessibility, 2)
        self.assertEqual(site.latitude, Decimal('55.750000'))
        self.assertEqual(site.longitude, Decimal('37.620000'))
        self.assertEqual(site.min_daily_price, Decimal('1000.00'))
        self.assertEqual(site.max_daily_price, Decimal('3000.00'))
        self.assertEqual(site.season, SEASON_WINTER)
        self.assertTrue(site.limited_mobility_access)
        self.assertEqual(site.rating, 5)

    def test_repeated_row_keeps_the_last_values(self):
        message = self.load(
            [
                row(
                    description='первое',
                    treatment_profile='первый профиль',
                    procedures='ванны',
                    excursions='море',
                    season='лето',
                    limited_mobility_access='0',
                    rating='2',
                    min_daily_price='1000.00',
                    max_daily_price='2000.00',
                    transport_accessibility='4',
                ),
                row(
                    description='второе',
                    treatment_profile='второй профиль',
                    procedures='грязи',
                    excursions='горы',
                    season='зима',
                    limited_mobility_access='да',
                    rating='5',
                    min_daily_price='3000.00',
                    max_daily_price='8000.00',
                    transport_accessibility='1',
                ),
            ]
        )

        self.assertIn('Учреждения: создано 1, обновлено 0.', message)
        self.assertIn('Площадки: создано 1, обновлено 0.', message)
        institution = Institution.objects.get()
        site = Site.objects.get()
        self.assertEqual(institution.description, 'второе')
        self.assertEqual(institution.treatment_profile, 'второй профиль')
        self.assertEqual(site.procedures, 'грязи')
        self.assertEqual(site.excursions, 'горы')
        self.assertEqual(site.transport_accessibility, 1)
        self.assertEqual(site.min_daily_price, Decimal('3000.00'))
        self.assertEqual(site.max_daily_price, Decimal('8000.00'))
        self.assertEqual(site.season, SEASON_WINTER)
        self.assertTrue(site.limited_mobility_access)
        self.assertEqual(site.rating, 5)

    def test_broken_row_rolls_back_the_whole_load(self):
        institution = make_institution(description='было', treatment_profile='Общий')
        site = make_site(institution, procedures='старые процедуры')

        with self.assertRaises(CommandError) as caught:
            self.load(
                [
                    row(
                        description='не должно сохраниться',
                        procedures='новые процедуры',
                        season='зима',
                        limited_mobility_access='1',
                        rating='5',
                    ),
                    row(
                        institution_name='Маяк',
                        address='Сочи',
                        season='никогда',
                        rating='4',
                    ),
                ]
            )

        self.assertIn('Строка 3', str(caught.exception))
        institution.refresh_from_db()
        site.refresh_from_db()
        self.assertEqual(institution.description, 'было')
        self.assertEqual(site.procedures, 'старые процедуры')
        self.assertEqual(site.season, SEASON_YEAR_ROUND)
        self.assertFalse(site.limited_mobility_access)
        self.assertEqual(site.rating, 0)
        self.assertFalse(Institution.objects.filter(name='Маяк').exists())
        self.assertEqual(Site.objects.count(), 1)

    def test_blank_optional_cells_use_defaults(self):
        self.load([row(season='зима', limited_mobility_access='1', rating='5')])
        self.load([row(season='', limited_mobility_access='', rating='')])

        site = Site.objects.get()
        self.assertEqual(site.season, SEASON_YEAR_ROUND)
        self.assertFalse(site.limited_mobility_access)
        self.assertEqual(site.rating, 0)

    def test_omitted_optional_columns_keep_existing_values(self):
        self.load([row(season='зима', limited_mobility_access='1', rating='5')])
        header = [name for name in FULL_HEADER if name not in OPTIONAL_HEADER]

        self.load([row(procedures='грязи')], header)

        site = Site.objects.get()
        self.assertEqual(site.procedures, 'грязи')
        self.assertEqual(site.season, SEASON_WINTER)
        self.assertTrue(site.limited_mobility_access)
        self.assertEqual(site.rating, 5)

    def test_builtin_demo_loads_new_fields(self):
        call_command('load_demo', stdout=StringIO())

        self.assertEqual(Institution.objects.count(), 2)
        self.assertEqual(Site.objects.count(), 3)
        keys = Site.objects.get(address='ул. Примерная, 1')
        shore = Site.objects.get(address='наб. Учебная, 2')
        lane = Site.objects.get(address='пер. Схемы, 3')
        self.assertEqual(keys.season, SEASON_YEAR_ROUND)
        self.assertTrue(keys.limited_mobility_access)
        self.assertEqual(keys.rating, 4)
        self.assertEqual(shore.season, SEASON_SUMMER)
        self.assertFalse(shore.limited_mobility_access)
        self.assertEqual(shore.rating, 3)
        self.assertEqual(lane.institution_id, shore.institution_id)
        self.assertEqual(lane.season, SEASON_WINTER)
        self.assertTrue(lane.limited_mobility_access)
        self.assertEqual(lane.rating, 5)

    def test_project_csv_loads_and_second_run_updates(self):
        call_command('load_demo', str(PROJECT_CSV), stdout=StringIO())
        output = StringIO()
        call_command('load_demo', str(PROJECT_CSV), stdout=output)

        self.assertEqual(Institution.objects.count(), 20)
        self.assertEqual(Site.objects.count(), 25)
        self.assertIn('Учреждения: создано 0, обновлено 20.', output.getvalue())
        self.assertIn('Площадки: создано 0, обновлено 25.', output.getvalue())
        borok = Site.objects.get(
            address='посёлок Борок, Жуковский район, Калужская область'
        )
        cottage = Site.objects.get(address__contains='домик в лесу')
        meadow = Site.objects.get(institution__name='Пансионат «Грибная поляна»')
        budget = Site.objects.get(institution__name='Пансионат «Белые ночи»')
        rehab = Site.objects.get(institution__name='Пансионат «Луговой»')
        self.assertEqual(borok.season, SEASON_YEAR_ROUND)
        self.assertTrue(borok.limited_mobility_access)
        self.assertEqual(borok.rating, 4)
        self.assertEqual(cottage.season, SEASON_YEAR_ROUND)
        self.assertFalse(cottage.limited_mobility_access)
        self.assertEqual(cottage.rating, 5)
        self.assertEqual(meadow.season, SEASON_SUMMER)
        self.assertFalse(meadow.limited_mobility_access)
        self.assertEqual(meadow.rating, 3)
        self.assertEqual(budget.season, SEASON_SUMMER)
        self.assertEqual(budget.rating, 2)
        self.assertTrue(rehab.limited_mobility_access)


class CatalogAdminTests(TestCase):
    def test_new_fields_and_sites_inside_institution(self):
        self.assertIn(SiteInline, InstitutionAdmin.inlines)
        for field_name in OPTIONAL_HEADER:
            self.assertIn(field_name, SiteAdmin.list_display)
            self.assertIn(field_name, SiteAdmin.list_filter)
            self.assertIn(field_name, SiteInline.fields)

        user = User.objects.create_superuser('admin', 'admin@example.com', 'secret-pass')
        self.client.force_login(user)
        institution = make_institution()
        site = make_site(
            institution,
            season=SEASON_SUMMER,
            limited_mobility_access=True,
            rating=3,
        )

        institution_page = self.client.get(
            reverse('admin:catalog_institution_change', args=[institution.pk])
        )
        site_page = self.client.get(reverse('admin:catalog_site_change', args=[site.pk]))

        self.assertEqual(institution_page.status_code, 200)
        self.assertContains(institution_page, 'sites-0-season')
        self.assertContains(institution_page, 'sites-0-limited_mobility_access')
        self.assertContains(institution_page, 'sites-0-rating')
        self.assertContains(institution_page, site.address)
        self.assertContains(institution_page, 'Доступность для маломобильных')
        self.assertEqual(site_page.status_code, 200)
        self.assertContains(site_page, 'name="season"')
        self.assertContains(site_page, 'name="limited_mobility_access"')
        self.assertContains(site_page, 'name="rating"')
