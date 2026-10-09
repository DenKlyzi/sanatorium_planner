"""Чипы услуг, цена без копеек и разбор текста маршрута."""

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


_ABBREVIATIONS = {
    'им', 'т', 'др', 'пр', 'ул', 'г', 'д', 'стр', 'корп', 'см', 'рис',
    'кв', 'пл', 'наб', 'пер', 'ш', 'обл', 'пос', 'проф', 'тыс', 'руб',
}

# Точка с пробелом — граница фразы, если перед ней не инициал и не сокращение.
# «К.Э.» и «им. Кирова» остаются в названии, «ортопеда. Лечебная» — две услуги.
_OPENERS = {'(', '[', '«'}
_CLOSERS = {')': '(', ']': '[', '»': '«'}

_CONSULT_HEAD = re.compile(r'консультаци(?:я|и|ю|ей)(?=\s)', re.IGNORECASE)
_SPECIALIST = re.compile(r'^[А-Яа-яЁёA-Za-z-]+(?:\s+[А-Яа-яЁёA-Za-z-]+){0,2}$')
_NOT_A_SPECIALIST = {
    'массаж', 'ванны', 'ванна', 'анализ', 'процедура', 'процедуры',
    'экскурсия', 'экскурсии', 'питание',
}

_GARBAGE_TOKEN = re.compile(r'из\s*[рr]?\s*p?rovided|\bprovided\b', re.IGNORECASE)
_RANGE_PRICE = re.compile(
    r'от\s+(\d[\d ]*)\s+до\s+(\d[\d ]*)\s*руб(?:\.|лей|ля)?',
    re.IGNORECASE,
)
_SINGLE_PRICE = re.compile(
    r'(\d[\d ]*)\s*руб(?:\.|лей|ля)?',
    re.IGNORECASE,
)

_LABELS = (
    ('как добраться', 'arrival'),
    ('день визита', 'day'),
    ('ключевые процедуры', 'skip'),
    ('общий бюджетный итог', 'summary'),
    ('площадка', 'name'),
    ('стоимость', 'skip'),
    ('процедуры', 'skip'),
    ('экскурсии', 'skip'),
    ('советы', 'tips'),
    ('итог', 'summary'),
    ('день', 'day'),
)


@dataclass(frozen=True)
class RouteStop:
    name: str
    day: str
    price: str
    transport: int
    arrival: str
    procedures: list[str]
    excursions: list[str]


@dataclass(frozen=True)
class RouteView:
    stops: list[RouteStop]
    summary: str
    tips: list[str]
    extra: str


def format_amount(value) -> str:
    """Целое число рублей с пробелом между тысячами, без копеек."""
    amount = Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    sign = '-' if amount < 0 else ''
    digits = str(abs(int(amount)))
    groups = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return sign + ' '.join(reversed(groups))


def format_price_range(low, high) -> str:
    """«2 900–7 400 ₽». Пустой диапазон — пустая строка."""
    if low is None and high is None:
        return ''
    left = format_amount(low) if low is not None else ''
    right = format_amount(high) if high is not None else ''
    if left and right and left != right:
        body = f'{left}–{right}'
    else:
        body = left or right
    return f'{body} ₽'


def split_phrases(value) -> list[str]:
    """Одна услуга — один пункт.

    Режем по запятой, точке с запятой и переносу строки.
    Точку внутри названия и запятую внутри скобок не трогаем.
    """
    if not value:
        return []
    text = str(value).replace('\r\n', '\n').replace('\r', '\n')
    text = _sentence_periods_to_semicolons(text)
    text = _expand_consultations(text)
    parts = []
    buf = []
    stack = []
    for char in text:
        if char in _OPENERS:
            stack.append(char)
            buf.append(char)
            continue
        if char in _CLOSERS and stack and stack[-1] == _CLOSERS[char]:
            stack.pop()
            buf.append(char)
            continue
        if not stack and char in ',;\n':
            _push_phrase(parts, ''.join(buf))
            buf = []
            continue
        buf.append(char)
    _push_phrase(parts, ''.join(buf))
    return parts


def clean_copy(value) -> str:
    """Убирает «изprovided», буквы чужих письменностей и «руб.» с копейками."""
    if not value:
        return ''
    text = str(value)
    text = (
        text.replace('\u00a0', ' ')
        .replace('\u202f', ' ')
        .replace('\u2009', ' ')
    )
    text = _GARBAGE_TOKEN.sub('', text)
    text = _drop_foreign_letters(text)
    text = _collapse_inline_space(text)
    text = re.sub(r'данные\s+источников', 'по данным каталога', text, flags=re.IGNORECASE)
    text = re.sub(
        r'возможность\s+индивидуальные',
        'возможность добавить индивидуальные',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r'\(\s*\)', '', text)
    text = _RANGE_PRICE.sub(lambda match: _priced_range(match.group(1), match.group(2)), text)
    text = _SINGLE_PRICE.sub(lambda match: f'{_priced(match.group(1))} ₽', text)
    text = _collapse_inline_space(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def present_route(route_text, sites) -> RouteView:
    parsed = _parse_route(clean_copy(route_text or ''))
    stops = []
    for index, site in enumerate(sites, start=1):
        arrival = parsed['arrivals'][index - 1] if index - 1 < len(parsed['arrivals']) else ''
        stops.append(RouteStop(
            name=site.institution.name,
            day=f'День {index}',
            price=format_price_range(site.min_daily_price, site.max_daily_price),
            transport=site.transport_accessibility,
            arrival=arrival,
            procedures=split_phrases(getattr(site, 'procedures', '')),
            excursions=split_phrases(getattr(site, 'excursions', '')),
        ))
    return RouteView(
        stops=stops,
        summary=parsed['summary'],
        tips=parsed['tips'],
        extra=parsed['extra'],
    )


def _sentence_periods_to_semicolons(text: str) -> str:
    chars = []
    index = 0
    while index < len(text):
        if text[index] == '.' and _is_phrase_boundary(text, index):
            chars.append(';')
        else:
            chars.append(text[index])
        index += 1
    return ''.join(chars)


def _is_phrase_boundary(text: str, index: int) -> bool:
    word = _word_before(text, index)
    if not word or len(word) == 1 or word.casefold() in _ABBREVIATIONS:
        return False
    cursor = index + 1
    while cursor < len(text) and text[cursor] in ' \t':
        cursor += 1
    return cursor < len(text) and (text[cursor].isupper() or text[cursor] in '«"')


def _word_before(text: str, index: int) -> str:
    start = index
    while start > 0 and (text[start - 1].isalpha() or text[start - 1] in '-‐‑'):
        start -= 1
    return text[start:index]


def _expand_consultations(text: str) -> str:
    pieces = []
    cursor = 0
    for match in _CONSULT_HEAD.finditer(text):
        # «консультации А, Б / В» — несколько человек. Единственное число — одна услуга.
        plural = match.group(0).casefold().endswith('и')
        names, end = _take_specialists(text, match.end(), plural=plural)
        if not names:
            continue
        pieces.append(text[cursor:match.start()])
        pieces.append(', '.join(f'консультация {name}' for name in names))
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces)


def _take_specialists(text: str, start: int, plural: bool) -> tuple[list[str], int]:
    names = []
    i = start
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return names, i
        sep_at, sep_kind = _find_separator(text, i)
        chunk = text[i:sep_at].strip()
        if not _is_specialist(chunk):
            if not names:
                return [], start
            return names, i
        names.append(chunk)
        if not plural or sep_kind not in {',', '/', 'и'}:
            return names, sep_at
        nxt = sep_at + (3 if sep_kind == 'и' else 1)
        peek = nxt
        while peek < len(text) and text[peek].isspace():
            peek += 1
        if peek >= len(text):
            return names, len(text)
        next_at, _next_kind = _find_separator(text, peek)
        if not _is_specialist(text[peek:next_at].strip()):
            return names, sep_at
        i = nxt


def _find_separator(text: str, start: int) -> tuple[int, str]:
    stack = []
    i = start
    while i < len(text):
        char = text[i]
        if char in _OPENERS:
            stack.append(char)
        elif char in _CLOSERS and stack and stack[-1] == _CLOSERS[char]:
            stack.pop()
        elif not stack:
            if char in ',/;':
                return i, char
            if text[i:i + 3].casefold() == ' и ':
                return i, 'и'
        i += 1
    return len(text), ''


def _is_specialist(chunk: str) -> bool:
    if not chunk or not _SPECIALIST.match(chunk):
        return False
    if chunk.isupper() and len(chunk) <= 6:
        return False
    words = [word.strip('-') for word in chunk.casefold().replace('ё', 'е').split()]
    if any(word.startswith('консультац') for word in words):
        return False
    if any(word in _NOT_A_SPECIALIST for word in words):
        return False
    return words[-1].endswith(('а', 'я', 'ого', 'его', 'ых', 'их', 'ой', 'ии'))


def _push_phrase(parts: list[str], raw: str) -> None:
    text = raw.strip()
    text = _strip_trailing_dot(text).strip(' \t;')
    if not text:
        return
    parts.append(_capitalize(text))


def _strip_trailing_dot(text: str) -> str:
    while text.endswith('.') and not _keep_final_dot(text):
        text = text[:-1].rstrip()
    return text


def _keep_final_dot(text: str) -> bool:
    word = _word_before(text, len(text) - 1)
    if not word:
        return False
    return len(word) == 1 or word.casefold() in _ABBREVIATIONS


def _capitalize(text: str) -> str:
    for index, char in enumerate(text):
        if char.isalpha():
            if char.islower():
                return text[:index] + char.upper() + text[index + 1:]
            return text
    return text


def _drop_foreign_letters(text: str) -> str:
    """Слово с чужими буквами убирается целиком, чтобы не оставалось «ть» от «추가ть»."""
    kept = []
    index = 0
    while index < len(text):
        if text[index].isalpha():
            end = index + 1
            while end < len(text) and text[end].isalpha():
                end += 1
            word = text[index:end]
            if all(_is_cyrillic_or_latin(char) for char in word):
                kept.append(word)
            index = end
            continue
        kept.append(text[index])
        index += 1
    return ''.join(kept)


def _is_cyrillic_or_latin(char: str) -> bool:
    return ('A' <= char <= 'Z') or ('a' <= char <= 'z') or ('\u0400' <= char <= '\u04FF')


def _collapse_inline_space(text: str) -> str:
    lines = [re.sub(r'[ \t]{2,}', ' ', line).strip() for line in text.split('\n')]
    return '\n'.join(lines)


def _priced(raw: str) -> str:
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return raw.strip()
    return format_amount(Decimal(digits))


def _priced_range(left: str, right: str) -> str:
    return f'{_priced(left)}–{_priced(right)} ₽'


def _parse_route(text: str) -> dict:
    arrivals = []
    current = None
    field = None
    summary_lines = []
    tip_lines = []
    extra_lines = []

    def start_stop():
        nonlocal current, field
        current = []
        arrivals.append(current)
        field = None

    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            continue
        key, rest = _match_label(line)
        if key == 'name':
            start_stop()
            continue
        if key == 'arrival':
            if current is None:
                start_stop()
            field = 'arrival'
            if rest:
                current.append(rest)
            continue
        if key == 'summary':
            field = 'summary'
            current = None
            if rest:
                summary_lines.append(rest)
            continue
        if key == 'tips':
            field = 'tips'
            current = None
            if rest:
                tip_lines.append(rest)
            continue
        if key in {'day', 'skip'}:
            if current is None:
                start_stop()
            field = 'skip'
            continue
        if _is_place_title(line):
            start_stop()
            continue
        if field == 'arrival' and current is not None:
            current.append(line)
        elif field == 'summary':
            summary_lines.append(line)
        elif field == 'tips':
            tip_lines.append(line)
        elif field == 'skip':
            continue
        else:
            extra_lines.append(line)

    return {
        'arrivals': [_capitalize(' '.join(lines).strip()) for lines in arrivals],
        'summary': _capitalize(' '.join(summary_lines).strip()),
        'tips': _split_tips(tip_lines),
        'extra': '\n'.join(extra_lines).strip(),
    }


def _match_label(line: str) -> tuple[str | None, str]:
    folded = line.casefold().replace('ё', 'е')
    for label, key in _LABELS:
        prefix = label + ':'
        if folded.startswith(prefix):
            return key, line.split(':', 1)[1].strip()
    return None, ''


def _is_place_title(line: str) -> bool:
    if ':' in line:
        return False
    folded = line.casefold().replace('ё', 'е')
    return folded.startswith(('санаторий', 'пансионат', 'детский санаторий'))


def _split_tips(lines: list[str]) -> list[str]:
    tips = []
    for line in lines:
        item = re.sub(r'^\s*(?:[-–—•]|\d+[.)])\s*', '', line).strip()
        if item:
            tips.append(_capitalize(item))
    return tips
