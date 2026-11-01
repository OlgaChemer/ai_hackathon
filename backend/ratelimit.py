"""Скользящие окна одного процесса: IP обязателен, аккаунт — после проверки сессии.

Cookie посетителя и X-Forwarded-For здесь не считаются идентификатором доверия.
При нескольких workers нужен общий счётчик (например, Redis).
"""
import time
from collections import deque

# Полное совпадение для создания диалога, префиксы только с конечным '/'.
LIMITS = [("POST", "/api/session", 10, 600), ("POST", "/api/chat/", 30, 60),
          ("POST", "/api/session/", 60, 60), ("POST", "/api/stt", 40, 60),
          ("POST", "/api/tts", 40, 60), ("POST", "/api/auth/", 20, 600),
          ("GET", "/api/phone", 30, 60), ("GET", "/api/map/tiles/", 300, 60)]
MAX_BUCKETS = 10_000
_hits: dict[tuple, tuple[deque, float]] = {}
_last_cleanup = 0.0


def check(method: str, path: str, ip: str, user_id: int | None = None, now: float | None = None) -> float | None:
    global _last_cleanup
    now = time.monotonic() if now is None else now
    if now - _last_cleanup >= 60:
        for key, (q, window) in list(_hits.items()):
            if not q or now - q[-1] >= window:
                del _hits[key]
        _last_cleanup = now
    for m, route, limit, window in LIMITS:
        matches = path.startswith(route) if route.endswith("/") else path == route
        if method != m or not matches:
            continue
        keys = [(route, "ip", ip)]
        if user_id is not None:
            keys.append((route, "user", user_id))
        queues = []
        for key in keys:
            if key not in _hits:
                if len(_hits) >= MAX_BUCKETS:
                    return 60.0  # ограничиваем память, не сбрасывая действующие лимиты
                _hits[key] = (deque(), window)
            q = _hits[key][0]
            while q and now - q[0] >= window:
                q.popleft()
            queues.append(q)
        waits = [window - (now - q[0]) for q in queues if len(q) >= limit]
        if waits:
            return max(1.0, max(waits))
        for q in queues:
            q.append(now)
        return None
    return None


def reset() -> None:
    global _last_cleanup
    _hits.clear()
    _last_cleanup = 0.0
