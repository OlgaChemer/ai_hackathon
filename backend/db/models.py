"""Схема базы.

Люди и диалоги   users, visitors, auth_sessions, chats, chat_events
Безопасность     security_events (попытки инъекций и утечек — agent/guard.py)
Квартиры         listings, listing_prices, listing_features
Обход площадки   tracked_searches (что клиенты искали), crawl_runs (журнал ночных обходов)

Объявление хранится целиком в listings.data (формат агента, см. sources/base.py); отдельными
колонками — только то, по чему ищем. Признаки («балкон», «школа в 1 км») — в listing_features:
значение + источник (cian — поле площадки, text — описание, map — OpenStreetMap, call — слова продавца)
и время, чтобы знать, когда пересчитать. Один раз посчитали — пользуются все, кто ищет похожее.

Время — UTC без часового пояса (utcnow()).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


# ================================================================ люди и диалоги


class User(Base):
    """Зарегистрированный пользователь. Пароль — только хеш (scrypt)."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class Visitor(Base):
    """Браузер (cookie vid). Диалоги до входа принадлежат ему; при входе переходят пользователю."""
    __tablename__ = "visitors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    """Вход пользователя. В cookie sid — случайный токен, в базе — только его SHA-256."""
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class Chat(Base):
    """Диалог с агентом. Профиль и память агента — с них диалог продолжается после паузы или перезапуска."""
    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    visitor_id: Mapped[str | None] = mapped_column(ForeignKey("visitors.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | completed | cancelled | … (done-статусы агента)
    profile: Mapped[dict | None] = mapped_column(JSON)
    memory: Mapped[list | None] = mapped_column(JSON)  # разговор агента с моделью (сообщения и действия) — для продолжения
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ChatEvent(Base):
    """Всё, что видел клиент, по порядку: по этой ленте диалог открывается заново."""
    __tablename__ = "chat_events"
    __table_args__ = (UniqueConstraint("chat_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ================================================================ квартиры


class Listing(Base):
    """Объявление. id — «площадка-номер» (cian-335167546), как в агенте."""
    __tablename__ = "listings"
    __table_args__ = (Index("ix_listings_search", "city", "rooms", "is_new", "status", "price"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    city: Mapped[str] = mapped_column(String(60))
    rooms: Mapped[int] = mapped_column(Integer)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[int] = mapped_column(Integer)
    okrug: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | removed
    data: Mapped[dict] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # последний раз в выдаче площадки
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    page_checked_at: Mapped[datetime | None] = mapped_column(DateTime)  # открывали страницу объявления
    page_alive: Mapped[bool | None] = mapped_column(Boolean)  # True — в продаже, False — снято, None — не удалось проверить
    page_note: Mapped[str | None] = mapped_column(String(160))


class ListingPrice(Base):
    """История цены: строка при каждом изменении. Снижения — аргумент в торге."""
    __tablename__ = "listing_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    price: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ListingFeature(Base):
    """Признак квартиры: balcony -> true (cian), poi:school -> {name, dist, lat, lng} | null (map)."""
    __tablename__ = "listing_features"

    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True, index=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(10))  # cian | text | map | call
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ================================================================ обход площадки


class TrackedSearch(Base):
    """Поиск, который клиенты уже задавали: ночной обход обновляет по нему базу."""
    __tablename__ = "tracked_searches"
    __table_args__ = (UniqueConstraint("city", "rooms", "is_new", "okrug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(60))
    rooms: Mapped[int] = mapped_column(Integer)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    okrug: Mapped[str] = mapped_column(String(20), default="")  # "" — весь город
    budget_max: Mapped[int | None] = mapped_column(Integer)    # самый большой бюджет, который спрашивали
    requested_count: Mapped[int] = mapped_column(Integer, default=0)
    last_requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime)


class CrawlRun(Base):
    """Один обход: по нему видно, жив ли сервис обновления и что он нашёл."""
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default="running")  # running | ok | partial | failed
    searches: Mapped[int] = mapped_column(Integer, default=0)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    price_changed: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSON)


# ================================================================ безопасность


class SecurityEvent(Base):
    """Попытка промпт-инъекции, утечки или злоупотребления (agent/guard.py): что, где, кто. Исходный текст не хранится: sample содержит только SHA-256 и длину."""
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str | None] = mapped_column(String(32), index=True)
    visitor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    where: Mapped[str] = mapped_column(String(20))      # input | seller | output | data
    category: Mapped[str] = mapped_column(String(20))   # exfiltration | override | persona | flood | code | offtopic | prompt_leak …
    sample: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
