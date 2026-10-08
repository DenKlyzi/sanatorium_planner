# TODO(team): подставить свой base URL прокси и модель, ключ только из окружения.

import os
import json

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from django.conf import settings

load_dotenv()

_TIMEOUT_SECONDS = 30
_ATTEMPTS = 2


def complete(system: str, user: str) -> str:
    """Возвращает текст ответа модели на сообщения system и user."""
    if not isinstance(system, str) or not isinstance(user, str):
        raise TypeError('system и user должны быть строками.')
    base_url = _required_text(os.getenv('LLM_BASE_URL'))
    api_key = _required_text(os.getenv('LLM_API_KEY'))
    model = _required_text(os.getenv('LLM_MODEL'))
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
            exc.close()
            last_error = exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError('Запрос к прокси не выполнен.')


def _required_text(name: str) -> str:
    value = getattr(settings, name, '')
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
