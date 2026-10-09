from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


TRANSPORT_ACCESSIBILITY_CHOICES = [(score, str(score)) for score in range(1, 6)]

SEASON_YEAR_ROUND = 'year_round'
SEASON_SPRING = 'spring'
SEASON_SUMMER = 'summer'
SEASON_AUTUMN = 'autumn'
SEASON_WINTER = 'winter'
SEASON_CODES = (
    SEASON_YEAR_ROUND,
    SEASON_SPRING,
    SEASON_SUMMER,
    SEASON_AUTUMN,
    SEASON_WINTER,
)
SEASON_CHOICES = [
    (SEASON_YEAR_ROUND, 'круглый год'),
    (SEASON_SPRING, 'весна'),
    (SEASON_SUMMER, 'лето'),
    (SEASON_AUTUMN, 'осень'),
    (SEASON_WINTER, 'зима'),
]

RATING_MIN = 0
RATING_MAX = 5
RATING_CHOICES = [(RATING_MIN, 'не указан')] + [
    (score, str(score)) for score in range(RATING_MIN + 1, RATING_MAX + 1)
]


class Institution(models.Model):
    name = models.CharField(
        'название',
        max_length=255,
        unique=True,
        error_messages={'unique': 'Учреждение с таким названием уже есть.'},
    )
    description = models.TextField('описание')
    treatment_profile = models.CharField('профиль лечения', max_length=255)

    class Meta:
        verbose_name = 'учреждение'
        verbose_name_plural = 'учреждения'
        ordering = ['name']

    def __str__(self):
        return self.name


class Site(models.Model):
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='sites',
        verbose_name='учреждение',
    )
    address = models.CharField('адрес', max_length=500)
    latitude = models.DecimalField(
        'широта',
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(Decimal('-90')), MaxValueValidator(Decimal('90'))],
    )
    longitude = models.DecimalField(
        'долгота',
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(Decimal('-180')), MaxValueValidator(Decimal('180'))],
    )
    transport_accessibility = models.PositiveSmallIntegerField(
        'транспортная доступность',
        choices=TRANSPORT_ACCESSIBILITY_CHOICES,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text='Оценка от 1 до 5.',
    )
    min_daily_price = models.DecimalField(
        'минимальная цена за сутки',
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))],
    )
    max_daily_price = models.DecimalField(
        'максимальная цена за сутки',
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))],
    )
    procedures = models.TextField('текст процедур')
    excursions = models.TextField('текст экскурсий')
    season = models.CharField(
        'сезон',
        max_length=32,
        choices=SEASON_CHOICES,
        default=SEASON_YEAR_ROUND,
        help_text='Когда площадка принимает гостей.',
    )
    limited_mobility_access = models.BooleanField(
        'доступность для маломобильных',
        default=False,
        help_text='Есть условия для людей с ограниченной подвижностью.',
    )
    rating = models.PositiveSmallIntegerField(
        'рейтинг',
        choices=RATING_CHOICES,
        default=RATING_MIN,
        validators=[MinValueValidator(RATING_MIN), MaxValueValidator(RATING_MAX)],
        help_text='Оценка от 0 до 5. 0 — рейтинг не указан.',
    )

    class Meta:
        verbose_name = 'площадка'
        verbose_name_plural = 'площадки'
        ordering = ['address']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(transport_accessibility__gte=1)
                & models.Q(transport_accessibility__lte=5),
                name='site_transport_accessibility_between_1_and_5',
            ),
            models.CheckConstraint(
                condition=models.Q(min_daily_price__lte=models.F('max_daily_price')),
                name='site_min_daily_price_lte_max_daily_price',
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90) & models.Q(latitude__lte=90),
                name='site_latitude_range',
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180) & models.Q(longitude__lte=180),
                name='site_longitude_range',
            ),
            models.CheckConstraint(
                condition=models.Q(rating__gte=RATING_MIN)
                & models.Q(rating__lte=RATING_MAX),
                name='site_rating_between_0_and_5',
                violation_error_message='Рейтинг должен быть от 0 до 5.',
            ),
            models.CheckConstraint(
                condition=models.Q(season__in=SEASON_CODES),
                name='site_season_known',
                violation_error_message='Сезон должен быть одним из известных значений.',
            ),
            models.UniqueConstraint(
                fields=['institution', 'address'],
                name='site_institution_address_uniq',
                violation_error_message='У этого учреждения уже есть площадка с таким адресом.',
            ),
        ]

    def __str__(self):
        return f'{self.institution}: {self.address}'

    def clean(self):
        super().clean()
        if (
            self.min_daily_price is not None
            and self.max_daily_price is not None
            and self.min_daily_price > self.max_daily_price
        ):
            raise ValidationError(
                {
                    'max_daily_price': (
                        'Максимальная цена за сутки не может быть меньше минимальной.'
                    ),
                }
            )
