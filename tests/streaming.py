"""Тестовый ASGI-адаптер: завершить SSE на нужном событии, не ждать бесконечный поток.

Starlette <1.9 буферизует ответ TestClient.stream() до завершения приложения.
Адаптер ограничивает только тестовое подключение; рабочий endpoint не меняется.
"""
import json

import anyio


class BoundedStream:
    def __init__(self, app):
        self.app = app
        self.stop = None

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or self.stop is None:
            return await self.app(scope, receive, send)
        stop = self.stop
        buffered = b''
        sse = False
        with anyio.CancelScope() as cancellation:
            async def bounded_send(message):
                nonlocal buffered, sse
                if message['type'] == 'http.response.start':
                    sse = any(k.lower() == b'content-type' and b'text/event-stream' in v
                              for k, v in message.get('headers', []))
                if sse and message['type'] == 'http.response.body':
                    buffered += message.get('body', b'')
                    while b'\n' in buffered:
                        line, buffered = buffered.split(b'\n', 1)
                        if line.startswith(b'data:') and stop(json.loads(line[5:])):
                            await send({**message, 'more_body': False})
                            cancellation.cancel()
                            return
                await send(message)
            await self.app(scope, receive, bounded_send)
