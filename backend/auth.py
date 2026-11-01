"""Пользователи и cookie.

vid — id браузера (uuid4, год). Ставится всем с первого запроса: диалоги до регистрации привязаны к нему,
      при входе переходят пользователю (repo.claim_visitor).
sid — вход: случайный токен на AUTH_SESSION_DAYS; в базе — только SHA-256 токена.
Обе cookie — HttpOnly, SameSite=Lax; Secure — при COOKIE_SECURE=true (за HTTPS).

Пароль — scrypt со своей солью. Перебор: 5 неверных паролей на почту — пауза 10 минут.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import timedelta

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .config import settings
from .db import repo
from .db.models import AuthSession, User, utcnow

VISITOR_COOKIE = "vid"
SESSION_COOKIE = "sid"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 8
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
FAIL_LIMIT, FAIL_WINDOW = 5, 600


# ------------------------------------------------------------------ пароли


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, digest = (stored or "").split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ защита от перебора

_fails: dict[str, list[float]] = {}


def _check_rate(email: str) -> None:
    now = time.time()
    _fails[email] = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW]
    if len(_fails[email]) >= FAIL_LIMIT:
        raise HTTPException(429, "Слишком много попыток входа. Попробуйте через 10 минут.")


# ------------------------------------------------------------------ cookie


def _set_cookie(response: Response, name: str, value: str, days: int) -> None:
    response.set_cookie(name, value, max_age=days * 86400, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


def _valid_uuid(v: str | None) -> str | None:
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None


async def visitor_middleware(request: Request, call_next):
    """Каждому браузеру — cookie vid (без записи в базу: посетитель появляется там с первым диалогом)."""
    vid = _valid_uuid(request.cookies.get(VISITOR_COOKIE))
    fresh = vid is None
    if fresh:
        vid = str(uuid.uuid4())
    request.state.vid = vid
    response = await call_next(request)
    if fresh:
        _set_cookie(response, VISITOR_COOKIE, vid, 365)
    return response


def visitor_id(request: Request) -> str | None:
    return getattr(request.state, "vid", None) or _valid_uuid(request.cookies.get(VISITOR_COOKIE))


# ------------------------------------------------------------------ пользователи


def public(u: User | None) -> dict | None:
    return {"id": u.id, "email": u.email, "name": u.name} if u else None


def _start(s: Session, user: User, request: Request, response: Response) -> dict:
    token = secrets.token_urlsafe(32)
    s.add(AuthSession(token_hash=_token_hash(token), user_id=user.id,
                      expires_at=utcnow() + timedelta(days=settings.auth_session_days)))
    user.last_login_at = utcnow()
    moved = repo.claim_visitor(s, visitor_id(request), user.id)
    _set_cookie(response, SESSION_COOKIE, token, settings.auth_session_days)
    return {"user": public(user), "moved_chats": moved}


def register(email: str, password: str, name: str | None, request: Request, response: Response) -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Проверьте почту")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Пароль — не короче {MIN_PASSWORD} символов")
    with db.session() as s:
        if s.scalar(select(User.id).where(User.email == email)):
            raise HTTPException(409, "Эта почта уже зарегистрирована — войдите")
        user = User(email=email, password_hash=hash_password(password), name=(name or "").strip()[:120] or None)
        s.add(user)
        s.flush()
        return _start(s, user, request, response)


def login(email: str, password: str, request: Request, response: Response) -> dict:
    email = email.strip().lower()
    _check_rate(email)
    with db.session() as s:
        user = s.scalar(select(User).where(User.email == email))
        if not user or not verify_password(password, user.password_hash):
            _fails.setdefault(email, []).append(time.time())
            raise HTTPException(401, "Неверная почта или пароль")
        _fails.pop(email, None)
        return _start(s, user, request, response)


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.session() as s:
            a = s.get(AuthSession, _token_hash(token))
            if a and a.revoked_at is None:
                a.revoked_at = utcnow()
    response.delete_cookie(SESSION_COOKIE, path="/")


def _user(s: Session, request: Request) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    a = s.get(AuthSession, _token_hash(token))
    if not a or a.revoked_at is not None or a.expires_at < utcnow():
        return None
    return s.get(User, a.user_id)


def current_user(request: Request) -> dict | None:
    return db.safe(lambda s: public(_user(s, request)))


def whoami(request: Request) -> tuple[str | None, int | None]:
    """(id браузера, id пользователя или None) — для доступа к диалогам."""
    uid = db.safe(lambda s: (lambda u: u.id if u else None)(_user(s, request)))
    return visitor_id(request), uid
