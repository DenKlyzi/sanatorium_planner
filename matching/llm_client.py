import os
import json

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from django.conf import settings

load_dotenv(settings.BASE_DIR / '.env', override=False)

_TIMEOUT_SECONDS = 30
_ATTEMPTS = 2


def complete(system: str, user: str) -> str:
    """Возвращает текст ответа модели на сообщения system и user."""
    if not isinstance(system, str) or not isinstance(user, str):
        raise TypeError('system и user должны быть строками.')
    base_url = _required_text('LLM_BASE_URL')
    api_key = _required_text('LLM_API_KEY')
    model = _required_text('LLM_MODEL')
    url = _chat_completions_url(base_url)
    body = json.dumps(
        {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
        },
        ensure_ascii=False,
    ).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    last_error: Exception | None = None
    for _attempt in range(_ATTEMPTS):
        request = Request(url, data=body, headers=headers, method='POST')
        try:
            with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
            return _message_text(json.loads(raw.decode('utf-8')))
        except HTTPError as exc:
            last_error = _wrap_http_error(exc, url)
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError('Запрос к прокси не выполнен.')


def embed(texts: list[str]) -> list[list[float]]:
    """Возвращает эмбеддинги списка текстов через /embeddings прокси."""
    if not isinstance(texts, list) or not all(
        isinstance(t, str) for t in texts
    ):
        raise TypeError('texts должен быть списком непустых строк.')
    base_url = _required_text('LLM_BASE_URL')
    api_key = _required_text('LLM_API_KEY')
    model = _required_text('EMBEDDING_MODEL_NAME')
    url = _embeddings_url(base_url)
    body = json.dumps(
        {'model': model, 'input': texts},
        ensure_ascii=False,
    ).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    last_error: Exception | None = None
    for _attempt in range(_ATTEMPTS):
        request = Request(url, data=body, headers=headers, method='POST')
        try:
            with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
            return _embeddings_list(
                json.loads(raw.decode('utf-8')), len(texts)
            )
        except HTTPError as exc:
            last_error = _wrap_http_error(exc, url)
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError('Запрос к прокси не выполнен.')


def _required_text(name: str) -> str:
    value = getattr(settings, name, '')
    if not (isinstance(value, str) and value.strip()):
        value = os.environ.get(name, '')
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(
        f'{name} не задан. Base URL прокси и модель подставляет команда, '
        'ключ берётся только из окружения.'
    )


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/chat/completions'):
        return base
    return f'{base}/chat/completions'


def _embeddings_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/embeddings'):
        return base
    return f'{base}/embeddings'


def _message_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError('Ответ прокси не является JSON-объектом.')
    try:
        content = payload['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('В ответе нет choices[0].message.content.') from exc
    if not isinstance(content, str):
        raise ValueError('Текст ответа модели не строка.')
    return content


def _embeddings_list(payload: object, expected: int) -> list[list[float]]:
    if not isinstance(payload, dict):
        raise ValueError('Ответ прокси не является JSON-объектом.')
    try:
        data = payload['data']
        if not isinstance(data, list) or len(data) != expected:
            raise ValueError
        result: list[list[float]] = []
        for item in data:
            vector = item['embedding']
            if not isinstance(vector, list):
                raise ValueError
            result.append([float(v) for v in vector])
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(
            'В ответе нет data[*].embedding нужной длины.'
        ) from exc


def _wrap_http_error(exc: HTTPError, url: str) -> ValueError:
    """Читает тело HTTP-ошибки и возвращает ValueError с человекочитаемым текстом."""
    try:
        raw = exc.read()
        exc.close()
        text = raw.decode('utf-8', errors='replace').strip()
    except Exception:
        text = ''
    detail = ''
    if text:
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                error = payload.get('error')
                if isinstance(error, dict):
                    msg = error.get('message') or ''
                    code = error.get('code') or ''
                    if isinstance(msg, str) and msg.strip():
                        detail = msg.strip()
                        if isinstance(code, str) and code.strip():
                            detail = f'{detail} [{code.strip()}]'
                elif isinstance(error, str) and error.strip():
                    detail = error.strip()
        except Exception:
            pass
        if not detail:
            snippet = text[:200]
            detail = snippet + ('…' if len(text) > 200 else '')
    status = getattr(exc, 'code', exc.status) or '?'
    msg = f'HTTP {status} {url}'
    if detail:
        msg = f'{msg}: {detail}'
    return ValueError(msg)
