import json
import os
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import SimpleTestCase, override_settings

from matching import llm_client

_PROXY = 'https://proxy.example/v1'
_SECRET = 'sk-test-DO_NOT_LEAK_9f3a'
_CHAT_MODEL = 'demo-chat'
_EMBED_MODEL = 'demo-embed'
_MISSING_KEY = (
    'LLM_API_KEY не задан. Base URL прокси и модель подставляет команда, '
    'ключ берётся только из окружения.'
)


class _JSONResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._raw


def _http_error(url, code, body):
    return HTTPError(url, code, 'error', None, BytesIO(body))


@override_settings(
    LLM_BASE_URL=_PROXY,
    LLM_API_KEY=_SECRET,
    LLM_MODEL=_CHAT_MODEL,
    EMBEDDING_MODEL_NAME=_EMBED_MODEL,
)
class LlmClientTests(SimpleTestCase):
    def test_complete_returns_model_text(self):
        seen = []

        def ok(request, timeout):
            seen.append((request, timeout))
            payload = {
                'choices': [
                    {'message': {'content': 'Подходит: ванны и море.'}},
                ],
            }
            return _JSONResponse(payload)

        with patch('matching.llm_client.urlopen', side_effect=ok):
            text = llm_client.complete('Ты помощник.', 'Нужны ванны.')

        self.assertEqual(text, 'Подходит: ванны и море.')
        self.assertEqual(len(seen), 1)
        request, timeout = seen[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.full_url, f'{_PROXY}/chat/completions')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), f'Bearer {_SECRET}')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(
            json.loads(request.data.decode('utf-8')),
            {
                'model': _CHAT_MODEL,
                'messages': [
                    {'role': 'system', 'content': 'Ты помощник.'},
                    {'role': 'user', 'content': 'Нужны ванны.'},
                ],
            },
        )

    def test_http_error_includes_body_and_omits_api_key(self):
        bodies = (
            (
                'json',
                json.dumps(
                    {
                        'error': {
                            'message': f'модель недоступна, ключ {_SECRET}',
                            'code': 'invalid_api_key',
                        },
                    },
                ).encode('utf-8'),
                'модель недоступна, ключ *** [invalid_api_key]',
            ),
            (
                'text',
                f'прокси отклонил ключ {_SECRET}'.encode('utf-8'),
                'прокси отклонил ключ ***',
            ),
        )
        calls = (
            (lambda: llm_client.complete('система', 'гость'), f'{_PROXY}/chat/completions'),
            (lambda: llm_client.embed(['ванны']), f'{_PROXY}/embeddings'),
        )
        for kind, raw, detail in bodies:
            for call, url in calls:
                with self.subTest(kind=kind, url=url):
                    seen = []

                    def fail(request, timeout, raw=raw):
                        seen.append((request, timeout))
                        raise _http_error(request.full_url, 401, raw)

                    with patch('matching.llm_client.urlopen', side_effect=fail):
                        with self.assertRaises(ValueError) as ctx:
                            call()

                    message = str(ctx.exception)
                    self.assertEqual(message, f'HTTP 401 {url}: {detail}')
                    self.assertNotIn(_SECRET, message)
                    self.assertEqual(len(seen), 2)
                    for request, timeout in seen:
                        self.assertEqual(timeout, 30)
                        self.assertEqual(request.full_url, url)
                        self.assertEqual(
                            request.get_header('Authorization'),
                            f'Bearer {_SECRET}',
                        )
                        self.assertNotIn(_SECRET, request.full_url)

    def test_embed_rejects_wrong_length_and_omits_api_key(self):
        texts = ['ванны', 'море']
        payloads = (
            {'data': [{'embedding': [0.1, 0.2]}]},
            {
                'data': [
                    {'embedding': [0.1, 0.2]},
                    {'embedding': [0.3, 0.4]},
                    {'embedding': [0.5, 0.6]},
                ],
            },
        )
        for payload in payloads:
            with self.subTest(count=len(payload['data'])):
                seen = []

                def ok(request, timeout, payload=payload):
                    seen.append((request, timeout))
                    return _JSONResponse(payload)

                with patch('matching.llm_client.urlopen', side_effect=ok):
                    with self.assertRaises(ValueError) as ctx:
                        llm_client.embed(texts)

                message = str(ctx.exception)
                self.assertEqual(
                    message,
                    'В ответе нет data[*].embedding нужной длины.',
                )
                self.assertNotIn(_SECRET, message)
                self.assertEqual(len(seen), 2)
                request, timeout = seen[0]
                self.assertEqual(timeout, 30)
                self.assertEqual(request.full_url, f'{_PROXY}/embeddings')
                self.assertEqual(
                    request.get_header('Authorization'),
                    f'Bearer {_SECRET}',
                )
                self.assertEqual(
                    json.loads(request.data.decode('utf-8')),
                    {'model': _EMBED_MODEL, 'input': texts},
                )

    def test_blank_api_key_raises_and_omits_the_value(self):
        for raw in ('', '   ', '\n\t'):
            with self.subTest(raw=repr(raw)):
                with self.settings(LLM_API_KEY=raw), patch.dict(
                    os.environ, {'LLM_API_KEY': raw},
                ), patch('matching.llm_client.urlopen') as urlopen:
                    for call in (
                        lambda: llm_client.complete('система', 'гость'),
                        lambda: llm_client.embed(['ванны']),
                    ):
                        with self.assertRaises(ValueError) as ctx:
                            call()
                        message = str(ctx.exception)
                        self.assertEqual(message, _MISSING_KEY)
                        self.assertNotIn(_SECRET, message)
                        if raw.strip() == '' and raw:
                            self.assertNotIn(raw, message)
                    urlopen.assert_not_called()
