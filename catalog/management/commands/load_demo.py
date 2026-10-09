import csv
import inspect
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import (
    SEASON_AUTUMN,
    SEASON_CHOICES,
    SEASON_SPRING,
    SEASON_SUMMER,
    SEASON_WINTER,
    SEASON_YEAR_ROUND,
    Institution,
    Site,
)

# Встроенные строки показывают колонки и связь «учреждение — несколько площадок».
DEMO_CSV = """\
institution_name,description,treatment_profile,address,latitude,longitude,transport_accessibility,min_daily_price,max_daily_price,procedures,excursions,season,limited_mobility_access,rating
Демо-санаторий «Ключи»,Пример описания учреждения для схемы CSV.,Заболевания опорно-двигательного аппарата,"ул. Примерная, 1",55.750000,37.620000,4,4500.00,9200.00,"Пример текста процедур: ванны, массаж.",Пример текста экскурсий: обзорная прогулка.,круглый год,1,4
Демо-санаторий «Озеро»,Пример второго учреждения с двумя площадками.,Сердечно-сосудистые заболевания,"наб. Учебная, 2",43.580000,39.720000,3,5100.00,11000.00,Пример текста процедур: ингаляции.,Пример текста экскурсий: поездка к смотровой площадке.,лето,0,3
Демо-санаторий «Озеро»,Пример второго учреждения с двумя площадками.,Сердечно-сосудистые заболевания,"пер. Схемы, 3",43.590000,39.730000,2,3900.00,7600.00,Пример текста процедур: лечебная физкультура.,Пример текста экскурсий: пеший маршрут по парку.,зима,1,5
"""

REQUIRED_COLUMNS = (
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
)


def _fold(value):
    return value.strip().casefold().replace('ё', 'е')


def _season_aliases():
    aliases = {}
    for code, label in SEASON_CHOICES:
        aliases[code] = code
        aliases[_fold(label)] = code
    aliases.update(
        {
            'круглогодично': SEASON_YEAR_ROUND,
            'круглогодичный': SEASON_YEAR_ROUND,
            'летний': SEASON_SUMMER,
            'зимний': SEASON_WINTER,
            'весенний': SEASON_SPRING,
            'осенний': SEASON_AUTUMN,
            'fall': SEASON_AUTUMN,
        }
    )
    return aliases


_SEASON_ALIASES = _season_aliases()
_TRUE_VALUES = {'1', 'true', 'yes', 'y', 'да'}
_FALSE_VALUES = {'0', 'false', 'no', 'n', 'нет'}


class Command(BaseCommand):
    """Загружает учреждения (Institution) и связанные площадки (Site) из CSV.

    Одна строка — одна площадка. Учреждение определяется по колонке
    institution_name: одинаковое название собирает площадки в один Institution.
    Если название или пара «учреждение + адрес» уже есть в базе, команда
    обновляет эту запись. Повтор строки в том же файле оставляет последние
    значения. Вся загрузка выполняется в одной транзакции: ошибка в любой
    строке отменяет уже прочитанные строки этого запуска.

    Кодировка UTF-8. Разделитель полей — запятая. Десятичный разделитель —
    точка. Первая строка — заголовок, лишние колонки игнорируются.

    Колонки:
    institution_name — название учреждения, до 255 символов.
    description — описание учреждения.
    treatment_profile — профиль лечения, до 255 символов.
    address — адрес площадки, до 500 символов.
    latitude — широта от -90 до 90, до 6 знаков после точки.
    longitude — долгота от -180 до 180, до 6 знаков после точки.
    transport_accessibility — целое число от 1 до 5.
    min_daily_price — минимальная цена за сутки, не меньше 0.
    max_daily_price — максимальная цена за сутки, не меньше минимальной.
    procedures — текст процедур.
    excursions — текст экскурсий.
    season — необязательный сезон: круглый год, весна, лето, осень или зима.
    Пустая ячейка — круглый год. Если колонки нет, при создании берётся
    круглый год, а при обновлении сезон не меняется.
    limited_mobility_access — необязательная доступность для маломобильных:
    да или нет, 1 или 0. Пустая ячейка — нет. Если колонки нет, при создании
    берётся нет, а при обновлении значение не меняется.
    rating — необязательный рейтинг, целое число от 0 до 5. 0 значит, что
    рейтинг не указан. Пустая ячейка — 0. Если колонки нет, при создании
    берётся 0, а при обновлении рейтинг не меняется.

    Без аргумента читаются встроенные демо-строки (python manage.py load_demo).
    Свой файл передаётся путём (python manage.py load_demo путь/к/файлу.csv).
    """

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_path',
            nargs='?',
            help='Путь к CSV. Без аргумента загружаются встроенные демо-строки.',
        )

    def handle(self, *args, **options):
        csv_text = self._read_csv(options['csv_path'])
        rows = self._parse_rows(csv_text)
        with transaction.atomic():
            institutions_created, institutions_updated, sites_created, sites_updated = (
                self._load(rows)
            )
        self.stdout.write(
            self.style.SUCCESS(
                f'Учреждения: создано {institutions_created}, '
                f'обновлено {institutions_updated}. '
                f'Площадки: создано {sites_created}, обновлено {sites_updated}.'
            )
        )

    def _read_csv(self, csv_path):
        if not csv_path:
            return DEMO_CSV
        path = Path(csv_path)
        if not path.is_file():
            raise CommandError(f'Файл не найден: {path}')
        try:
            return path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError as exc:
            raise CommandError(f'Файл {path} должен быть в кодировке UTF-8.') from exc

    def _parse_rows(self, csv_text):
        reader = csv.DictReader(StringIO(csv_text))
        if not reader.fieldnames:
            raise CommandError('В CSV нет строки заголовка.')
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
        missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise CommandError('В CSV нет колонок: ' + ', '.join(missing) + '.')
        rows = []
        for line_number, row in enumerate(reader, start=2):
            if row is None or all(
                value is None or str(value).strip() == '' for value in row.values()
            ):
                continue
            rows.append((line_number, row))
        return rows

    def _load(self, rows):
        institutions_created = 0
        institutions_updated = 0
        sites_created = 0
        sites_updated = 0
        seen_institutions = {}
        seen_sites = {}

        for line_number, row in rows:
            name = self._text(row, 'institution_name', line_number)
            description = self._text(row, 'description', line_number)
            treatment_profile = self._text(row, 'treatment_profile', line_number)
            address = self._text(row, 'address', line_number)
            values = {
                'latitude': self._decimal(row, 'latitude', line_number),
                'longitude': self._decimal(row, 'longitude', line_number),
                'transport_accessibility': self._integer(
                    row, 'transport_accessibility', line_number
                ),
                'min_daily_price': self._decimal(row, 'min_daily_price', line_number),
                'max_daily_price': self._decimal(row, 'max_daily_price', line_number),
                'procedures': self._text(row, 'procedures', line_number),
                'excursions': self._text(row, 'excursions', line_number),
            }
            if 'season' in row:
                values['season'] = self._season(row, line_number)
            if 'limited_mobility_access' in row:
                values['limited_mobility_access'] = self._bool(
                    row, 'limited_mobility_access', line_number
                )
            if 'rating' in row:
                values['rating'] = self._optional_integer(row, 'rating', line_number)

            if name in seen_institutions:
                institution = seen_institutions[name]
            else:
                institution = self._one(
                    Institution.objects.filter(name=name),
                    line_number,
                    f'учреждение «{name}»',
                )
                if institution is None:
                    institution = Institution(name=name)
                    institutions_created += 1
                else:
                    institutions_updated += 1
                seen_institutions[name] = institution
            institution.description = description
            institution.treatment_profile = treatment_profile
            self._save(institution, line_number)

            site_key = (institution.pk, address)
            if site_key in seen_sites:
                site = seen_sites[site_key]
            else:
                site = self._one(
                    Site.objects.filter(institution=institution, address=address),
                    line_number,
                    f'площадка «{address}»',
                )
                if site is None:
                    site = Site(institution=institution, address=address)
                    sites_created += 1
                else:
                    sites_updated += 1
                seen_sites[site_key] = site
            for field, value in values.items():
                setattr(site, field, value)
            self._save(site, line_number)

        return institutions_created, institutions_updated, sites_created, sites_updated

    def _cell(self, row, column):
        value = row.get(column)
        if value is None:
            return ''
        return str(value).strip()

    def _default(self, field_name):
        return Site._meta.get_field(field_name).get_default()

    def _season(self, row, line_number):
        raw = self._cell(row, 'season')
        if raw == '':
            return self._default('season')
        code = _SEASON_ALIASES.get(_fold(raw))
        if code is None:
            labels = ', '.join(label for _code, label in SEASON_CHOICES)
            raise CommandError(
                f'Строка {line_number}: поле season должно быть одним из: {labels}.'
            )
        return code

    def _bool(self, row, column, line_number):
        raw = self._cell(row, column)
        if raw == '':
            return self._default(column)
        folded = _fold(raw)
        if folded in _TRUE_VALUES:
            return True
        if folded in _FALSE_VALUES:
            return False
        raise CommandError(
            f'Строка {line_number}: поле {column} должно быть да или нет ({raw}).'
        )

    def _optional_integer(self, row, column, line_number):
        raw = self._cell(row, column)
        if raw == '':
            return self._default(column)
        try:
            return int(raw)
        except ValueError as exc:
            raise CommandError(
                f'Строка {line_number}: поле {column} не является целым числом ({raw}).'
            ) from exc

    def _text(self, row, column, line_number):
        value = row.get(column)
        if value is None or value.strip() == '':
            raise CommandError(f'Строка {line_number}: пустое поле {column}.')
        return value.strip()

    def _decimal(self, row, column, line_number):
        raw = self._text(row, column, line_number)
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise CommandError(
                f'Строка {line_number}: поле {column} не является числом ({raw}).'
            ) from exc

    def _integer(self, row, column, line_number):
        raw = self._text(row, column, line_number)
        try:
            return int(raw)
        except ValueError as exc:
            raise CommandError(
                f'Строка {line_number}: поле {column} не является целым числом ({raw}).'
            ) from exc

    def _one(self, queryset, line_number, label):
        matches = list(queryset[:2])
        if len(matches) > 1:
            raise CommandError(
                f'Строка {line_number}: в базе несколько записей ({label}).'
            )
        if matches:
            return matches[0]
        return None

    def _save(self, instance, line_number):
        try:
            instance.full_clean()
        except ValidationError as exc:
            raise CommandError(f'Строка {line_number}: {self._validation_text(exc)}') from exc
        instance.save()

    def _validation_text(self, exc):
        if hasattr(exc, 'error_dict'):
            parts = []
            for field, errors in exc.error_dict.items():
                for error in errors:
                    parts.append(f'{field}: {error.message}')
            return '; '.join(parts)
        return '; '.join(error.message for error in exc.error_list)


Command.help = inspect.cleandoc(Command.__doc__)
