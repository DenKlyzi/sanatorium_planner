"""Подбор площадок: фильтр по бюджету и региону, затем взвешенная оценка.

Бюджет — потолок цены за сутки: площадка остаётся, если минимальная цена
не выше бюджета. Регион ищется в адресе без учёта регистра, «е» и «ё»
считаются одним знаком. Сезон и доступность для маломобильных режут выдачу
только если их явно передали отдельными аргументами match. Цена, транспорт
и рейтинг в эмбеддинг не входят, они только слагаемые score. Вектор площадки
сохраняется по хешу её текста и имени модели эмбеддингов.
"""

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings

from catalog.models import SEASON_CHOICES, SEASON_YEAR_ROUND, Site
from matching import llm_client
from matching.models import SiteEmbedding

# Вектор запроса не привязан к площадке, поэтому живёт только в процессе.
# Ключ: (sha256 запроса, имя модели эмбеддингов).
_QUERY_VECTORS: dict[tuple[str, str], list[float]] = {}

_TOP_N = 5
_MIN_SCORE = 0.15

# Сумма весов равна 1. Рейтинг — небольшой вес, снятый с похожести.
_W_SIMILARITY = 0.45
_W_PROCEDURES = 0.2
_W_EXCURSIONS = 0.1
_W_TRANSPORT = 0.1
_W_PRICE = 0.1
_W_RATING = 0.05

# Код сезона и русская подпись из каталога → канонический код.
_SEASON_LOOKUP = {
    folded: code
    for code, label in SEASON_CHOICES
    for folded in (code.casefold(), label.casefold())
}

_STOP_WORDS = {
    'и', 'в', 'на', 'с', 'для', 'по', 'от', 'до', 'не', 'или', 'а',
    'но', 'что', 'как', 'это', 'мне', 'мы', 'вы', 'он', 'она', 'они',
    'хочу', 'нужно', 'можно', 'быть', 'есть',
}

EXPLAIN_SYSTEM_PROMPT = (
    'Ты менеджер по подбору санаториев и площадок для отдыха и лечения. '
    'Объясни пользователю на русском языке, почему выбранная площадка '
    'соответствует его пожеланиям. Отметь совпадения по бюджету, региону, '
    'процедурам и экскурсиям. Пробелы в совпадении назови прямо: '
    'если бюджет, регион, процедура или экскурсия не совпадают, '
    'скажи, чего именно нет. '
    'Формат ответа: сплошной текст из 3–6 предложений. '
    'Не используй Markdown: никаких **, ##, списков через дефис, таблиц и ссылок. '
    'Не используй вводные фразы вроде «конечно», «здравствуйте», «разумеется». '
    'Все числа (цены, сроки, длительность курса, расстояния) пиши цифрами, '
    'а не словами. '
    'Не повторяй название площадки больше одного раза. '
    'Бери только факты из сообщения пользователя.'
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
    """Текст для эмбеддинга: название, профиль, описание, адрес, процедуры, экскурсии.

    Без подписей. Пустые и пробельные части пропускаются. Цена и транспорт
    сюда не входят.
    """
    institution = site.institution
    parts = (
        institution.name,
        institution.treatment_profile,
        institution.description,
        site.address,
        site.procedures,
        site.excursions,
    )
    return '\n'.join(part.strip() for part in parts if part and str(part).strip())


def match(
    preferences: Preferences,
    *,
    encoder=None,
    season: str | None = None,
    limited_mobility_access: bool | None = None,
) -> list[ScoredSite]:
    """Возвращает до 5 площадок с итоговым score.

    season и limited_mobility_access необязательны и по умолчанию выдачу
    не режут. Сезон — код или подпись из каталога. Запрос конкретного сезона
    оставляет и площадки «круглый год»: они открыты в этот сезон. Запрос
    «круглый год» оставляет только такие площадки. True у маломобильности
    оставляет только доступные площадки, False — только недоступные.
    """
    sites = _filter_sites(
        preferences.budget,
        preferences.region,
        season=season,
        limited_mobility_access=limited_mobility_access,
    )
    return _rank(preferences.query, sites, encoder, budget=preferences.budget)


def explain(preferences: Preferences, site: Site) -> str:
    """Просит модель пояснить, почему площадка подходит под пожелания."""
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


def _fold_yo(text: str) -> str:
    """Нижний регистр, где «ё» и «е» — один знак. Для поиска региона."""
    return text.casefold().replace('ё', 'е')


def _filter_sites(
    budget: Decimal,
    region: str,
    *,
    season: str | None = None,
    limited_mobility_access: bool | None = None,
) -> list[Site]:
    # Проверяем явные аргументы до запроса: пустой каталог не прячет ошибку.
    season_codes = _open_seasons(season) if season is not None else None
    access = (
        _mobility_flag(limited_mobility_access)
        if limited_mobility_access is not None
        else None
    )
    queryset = Site.objects.select_related('institution').filter(
        min_daily_price__lte=budget,
    )
    if season_codes is not None:
        queryset = queryset.filter(season__in=season_codes)
    if access is not None:
        queryset = queryset.filter(limited_mobility_access=access)
    folded = _fold_yo(region)
    if not folded:
        return list(queryset)
    return [site for site in queryset if folded in _fold_yo(site.address)]


def _known_season(value) -> str:
    """Код или подпись сезона. Пустая строка и неизвестное значение — ошибка."""
    if not isinstance(value, str):
        raise TypeError('Сезон должен быть строкой.')
    code = _SEASON_LOOKUP.get(value.strip().casefold())
    if code is None:
        raise ValueError('Сезон должен быть одним из известных значений.')
    return code


def _open_seasons(season: str) -> tuple[str, ...]:
    """Какие сезоны площадки открыты в запрошенный сезон заезда."""
    code = _known_season(season)
    if code == SEASON_YEAR_ROUND:
        return (SEASON_YEAR_ROUND,)
    return (code, SEASON_YEAR_ROUND)


def _mobility_flag(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError(
            'Доступность для маломобильных должна быть логическим значением.'
        )
    return value


def _rank(
    query: str,
    sites: list[Site],
    encoder,
    *,
    budget: Decimal,
) -> list[ScoredSite]:
    if not sites:
        return []
    if encoder is None:
        vectors = _cached_embeddings(query, sites)
    else:
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
            + _W_RATING * _rating_score(site.rating)
        )

        if score < _MIN_SCORE:
            continue
        ranked.append(ScoredSite(site=site, score=score))
    ranked.sort(key=lambda item: (-item.score, item.site.pk))
    return ranked[:_TOP_N]


def _content_words(text: str | None) -> set[str]:
    """Значимые слова: нижний регистр, без краевой пунктуации и стоп-слов."""
    if not text:
        return set()
    words = set()
    for raw in str(text).split():
        word = raw.strip('.,;:!?()«»"\'').lower()
        if len(word) > 2 and word not in _STOP_WORDS:
            words.add(word)
    return words


def _text_overlap(query: str, field_value: str | None) -> float:
    """Доля значимых слов запроса, которые есть отдельными словами в поле.

    Возвращает 0..1. Пустой запрос возвращает 1: площадка не штрафуется.
    Непустой запрос и пустое поле возвращают 0.
    """
    query_words = _content_words(query)
    if not query_words:
        return 1.0
    return len(query_words & _content_words(field_value)) / len(query_words)


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


def _rating_score(rating) -> float:
    """Рейтинг 1..5 → 0..1. 0 и пустое значение — «не указан», нейтральные 0.5.

    Площадка без рейтинга не получает ни бонус пятёрки, ни штраф единицы.
    """
    if isinstance(rating, bool) or rating is None:
        return 0.5
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return 0.5
    if rating <= 0:
        return 0.5
    rating = min(rating, 5)
    return (rating - 1) / 4.0


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


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _embedding_model_name() -> str:
    name = getattr(settings, 'EMBEDDING_MODEL_NAME', '')
    if not isinstance(name, str):
        return ''
    return name.strip()


def _cached_embeddings(query: str, sites: list[Site]) -> list[list[float]]:
    """Возвращает [вектор запроса, *векторы площадок] — тот же порядок, что у _encode.

    Попадание: в SiteEmbedding есть эта площадка, sha256 от site_text и текущее
    имя модели. Промахи уходят в llm_client.embed одним списком. Если промахов
    нет и вектор запроса уже в памяти, embed не вызывается.
    """
    model_name = _embedding_model_name()
    prepared: list[tuple[Site, str, str]] = []
    for site in sites:
        text = site_text(site)
        prepared.append((site, text, _text_hash(text)))

    stored = {
        (row.site_id, row.text_hash): _vector(row.vector)
        for row in SiteEmbedding.objects.filter(
            site_id__in=[site.pk for site, _text, _digest in prepared],
            model_name=model_name,
            text_hash__in={digest for _site, _text, digest in prepared},
        )
    }
    site_vectors: dict[int, list[float]] = {}
    missed: list[tuple[Site, str, str]] = []
    for site, text, digest in prepared:
        vector = stored.get((site.pk, digest))
        if vector is None:
            missed.append((site, text, digest))
        else:
            site_vectors[site.pk] = vector

    query_key = (_text_hash(query), model_name)
    query_vector = _QUERY_VECTORS.get(query_key)
    if query_vector is None or missed:
        to_embed: list[str] = []
        if query_vector is None:
            to_embed.append(query)
        to_embed.extend(text for _site, text, _digest in missed)
        raw = _encode(to_embed, None)
        if len(raw) != len(to_embed):
            raise ValueError('Энкодер вернул неверное число эмбеддингов.')
        offset = 0
        if query_vector is None:
            query_vector = raw[0]
            _QUERY_VECTORS[query_key] = query_vector
            offset = 1
        for (site, _text, digest), vector in zip(missed, raw[offset:], strict=True):
            SiteEmbedding.objects.update_or_create(
                site=site,
                text_hash=digest,
                model_name=model_name,
                defaults={'vector': vector},
            )
            site_vectors[site.pk] = vector

    return [query_vector, *[site_vectors[site.pk] for site, _text, _digest in prepared]]


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