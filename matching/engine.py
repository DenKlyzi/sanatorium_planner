"""Подбор площадок: фильтр по бюджету и региону, затем косинусная близость.

Бюджет — максимальная цена за сутки: площадка остаётся, если её минимальная
цена не выше бюджета. Регион ищется в адресе без учёта регистра.
"""

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings

from catalog.models import Site
from matching import llm_client

_TOP_N = 5
_MIN_SCORE = 0.15

EXPLAIN_SYSTEM_PROMPT = (
    'Ты менеджер по подбору санаториев и площадок для отдыха и лечения. '
    'Кратко, по пунктам, объясни пользователю на русском языке, '
    'почему выбранная площадка соответствует его пожеланиям. '
    'Отдельно отметь совпадения по бюджету, региону, процедурам и экскурсиям. '
    'Если чего-то не хватает — честно скажи об этом. '
    'Ответ 3–6 предложений, без вводных фраз вроде «конечно» или «здравствуйте».'
)


def _budget(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError('Бюджет должен быть неотрицательным числом.')
    try:
        budget = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Бюджет должен быть неотрицательным числом.') from exc
    if not budget.is_finite() or budget < 0:
        raise ValueError('Бюджет должен быть неотрицательным числом.')
    return budget


def _plain_text(value, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{label} должен быть строкой.')
    return value.strip()


@dataclass(frozen=True)
class Preferences:
    """Пожелания гостя: потолок цены за сутки, регион и текст для эмбеддинга."""

    budget: Decimal
    region: str
    query: str

    def __post_init__(self):
        object.__setattr__(self, 'budget', _budget(self.budget))
        object.__setattr__(self, 'region', _plain_text(self.region, 'Регион'))
        object.__setattr__(self, 'query', _plain_text(self.query, 'Текст запроса'))


@dataclass(frozen=True)
class ScoredSite:
    site: Site
    score: float


def site_text(site: Site) -> str:
    """Склеивает информацию о площадке в текст для эмбеддинга."""
    parts = (
        site.institution.name,
        site.institution.treatment_profile,
        site.address,
        site.institution.description,
        f'Цена за сутки: {site.min_daily_price}–{site.max_daily_price}',
        f'Транспортная доступность: {site.transport_accessibility}',
        f'Процедуры: {site.procedures}',
        f'Экскурсии: {site.excursions}',
    )
    return '\n'.join(part.strip() for part in parts if part and str(part).strip())


def match(preferences: Preferences, *, encoder=None) -> list[ScoredSite]:
    """Возвращает до 5 площадок с score косинусной близости."""
    sites = _filter_sites(preferences.budget, preferences.region)
    return _rank(preferences.query, sites, encoder)


def explain(preferences: Preferences, site: Site) -> str:
    """Заглушка: просит llm_client.complete пояснить выбор площадки."""
    user_text = _explain_user_text(preferences, site)
    return llm_client.complete(EXPLAIN_SYSTEM_PROMPT, user_text)


def _explain_user_text(preferences: Preferences, site: Site) -> str:
    return '\n'.join(
        (
            f'Бюджет: {preferences.budget}',
            f'Регион: {preferences.region}',
            f'Запрос: {preferences.query}',
            f'Учреждение: {site.institution.name}',
            f'Описание: {site.institution.description}',
            f'Адрес: {site.address}',
            f'Транспортная доступность: {site.transport_accessibility}',
            f'Цена за сутки: {site.min_daily_price}–{site.max_daily_price}',
            f'Процедуры: {site.procedures}',
            f'Экскурсии: {site.excursions}',
        )
    )


def _filter_sites(budget: Decimal, region: str) -> list[Site]:
    queryset = Site.objects.select_related('institution').filter(
        min_daily_price__lte=budget,
    )
    folded = region.casefold()
    if not folded:
        return list(queryset)
    # SQLite не меняет регистр кириллицы в LIKE, поэтому регион сравниваем в Python.
    return [site for site in queryset if folded in site.address.casefold()]


def _rank(query: str, sites: list[Site], encoder) -> list[ScoredSite]:
    if not sites:
        return []
    vectors = _encode([query, *[site_text(site) for site in sites]], encoder)
    if len(vectors) != len(sites) + 1:
        raise ValueError('Энкодер вернул неверное число эмбеддингов.')
    query_vector = vectors[0]
    ranked: list[ScoredSite] = []
    for site, site_vector in zip(sites, vectors[1:], strict=True):
        similarity = _cosine(query_vector, site_vector)
        # TODO(team): score = w_procedures * процедуры + w_excursions * экскурсии + w_transport * транспорт + w_price * цена. Коэффициенты задаём сами.
        score = similarity
        if score < _MIN_SCORE:
            continue
        ranked.append(ScoredSite(site=site, score=score))
    ranked.sort(key=lambda item: (-item.score, item.site.pk))
    return ranked[:_TOP_N]


def _encode(texts: list[str], encoder) -> list[list[float]]:
    if encoder is None:
        raw = llm_client.embed(texts)
    else:
        raw = encoder.encode(texts)
    return [_vector(item) for item in raw]


def _vector(raw) -> list[float]:
    return [float(value) for value in raw]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError('Эмбеддинги должны быть непустыми и одной длины.')
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
