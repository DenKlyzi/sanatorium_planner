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

# Веса компонентов итогового score. Сумма = 1.0.
_W_SIMILARITY = 0.5
_W_PROCEDURES = 0.2
_W_EXCURSIONS = 0.1
_W_TRANSPORT  = 0.1
_W_PRICE      = 0.1

_STOP_WORDS = {
    'и', 'в', 'на', 'с', 'для', 'по', 'от', 'до', 'не', 'или', 'а',
    'но', 'что', 'как', 'это', 'мне', 'мы', 'вы', 'он', 'она', 'они',
    'хочу', 'нужно', 'можно', 'быть', 'есть',
}

EXPLAIN_SYSTEM_PROMPT = (
    'Ты менеджер по подбору санаториев и площадок для отдыха и лечения. '
    'Объясни пользователю на русском языке, почему выбранная площадка '
    'соответствует его пожеланиям. Отметь совпадения по бюджету, региону, '
    'процедурам и экскурсиям. Если чего-то не хватает — честно скажи об этом. '
    'Формат ответа: сплошной текст из 3–6 предложений. '
    'Не используй Markdown: никаких **, ##, списков через дефис, таблиц и ссылок. '
    'Не используй вводные фразы вроде «конечно», «здравствуйте», «разумеется». '
    'Все числа (цены, сроки, длительность курса, расстояния) пиши цифрами, '
    'а не словами. '
    'Не повторяй название площадки больше одного раза.'
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
    """Возвращает до 5 площадок с итоговым score."""
    sites = _filter_sites(preferences.budget, preferences.region)
    return _rank(preferences.query, sites, encoder, budget=preferences.budget)


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
    return [site for site in queryset if folded in site.address.casefold()]


def _rank(
    query: str,
    sites: list[Site],
    encoder,
    *,
    budget: Decimal,
) -> list[ScoredSite]:
    if not sites:
        return []
    vectors = _encode([query, *[site_text(site) for site in sites]], encoder)
    if len(vectors) != len(sites) + 1:
        raise ValueError('Энкодер вернул неверное число эмбеддингов.')
    query_vector = vectors[0]
    ranked: list[ScoredSite] = []
    for site, site_vector in zip(sites, vectors[1:], strict=True):
        similarity = _cosine(query_vector, site_vector)

        score = (
            _W_SIMILARITY * similarity
            + _W_PROCEDURES * _text_overlap(query, site.procedures)
            + _W_EXCURSIONS * _text_overlap(query, site.excursions)
            + _W_TRANSPORT * _transport_score(site.transport_accessibility)
            + _W_PRICE * _price_score(site.min_daily_price, budget)
        )

        if score < _MIN_SCORE:
            continue
        ranked.append(ScoredSite(site=site, score=score))
    ranked.sort(key=lambda item: (-item.score, item.site.pk))
    return ranked[:_TOP_N]


def _text_overlap(query: str, field_value: str | None) -> float:
    """Доля значимых слов запроса, которые встречаются в поле площадки.

    Возвращает 0..1. Грубая, но рабочая эвристика для процедур и экскурсий,
    так как в Preferences нет структурированных предпочтений по ним.
    """
    if not query or not field_value:
        return 0.0
    words = {
        w.strip('.,;:!?()«»"\'').lower()
        for w in query.split()
        if len(w) > 2 and w.lower() not in _STOP_WORDS
    }
    if not words:
        return 0.0
    haystack = str(field_value).lower()
    hits = sum(1 for w in words if w in haystack)
    return hits / len(words)


def _transport_score(level) -> float:
    """Транспортная доступность 1..5 → 0..1."""
    if level is None:
        return 0.0
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 0.0
    level = max(1, min(5, level))
    return (level - 1) / 4.0


def _price_score(price, budget: Decimal) -> float:
    """Чем дешевле относительно бюджета, тем выше. 0..1."""
    if price is None or budget <= 0:
        return 0.5  # нет данных — нейтрально
    try:
        price = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return 0.5
    if price > budget:
        return 0.0
    return float(1 - (price / budget))


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