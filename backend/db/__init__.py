"""База данных: SQLAlchemy 2. По умолчанию SQLite (backend/data/app.db), в проде — PostgreSQL через DATABASE_URL.

Схема — models.py, запросы — repo.py. Таблицы создаются при старте; новые колонки добавляются
в существующие таблицы автоматически (для прототипа вместо Alembic).

Работа синхронная: SQLite отвечает за миллисекунды. Если база недоступна, агент продолжает работать
без записи — см. safe().
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

log = logging.getLogger(__name__)
T = TypeVar("T")

_engine: Engine | None = None
_factory: sessionmaker | None = None


def configure(url: str | None = None) -> Engine:
    """Подключиться к базе и создать таблицы. url по умолчанию — DATABASE_URL из .env."""
    global _engine, _factory
    from ..config import settings

    url = url or settings.database_url
    kw: dict[str, Any] = {}
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ""
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:  # база в памяти (тесты): одно соединение на всех, иначе каждый увидит пустую базу
            from sqlalchemy.pool import StaticPool
            kw["poolclass"] = StaticPool
        kw["connect_args"] = {"check_same_thread": False}
    if _engine is not None:
        _engine.dispose()
    engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        in_memory = ":memory:" in url or url in ("sqlite://", "sqlite:///")

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):  # внешние ключи; WAL — чтение не ждёт записи (сервер + обход объявлений)
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    _engine, _factory = engine, sessionmaker(engine, expire_on_commit=False)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Новые колонки моделей — в уже созданные таблицы (без Alembic; хватает для прототипа)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"))
                    log.info("база: добавлена колонка %s.%s", table.name, col.name)


@contextmanager
def session() -> Iterator[Session]:
    """with db.session() as s: ... — коммит в конце, откат при ошибке."""
    if _factory is None:
        configure()
    s = _factory()  # type: ignore[misc]
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def safe(fn: Callable[..., T], *args: Any, default: T | None = None, **kw: Any) -> T | None:
    """fn(db, *args) в своей транзакции. Ошибка базы не останавливает агента: пишем в лог и возвращаем default."""
    try:
        with session() as s:
            return fn(s, *args, **kw)
    except Exception:  # noqa: BLE001 — база вспомогательная: без неё агент работает, только без истории
        log.exception("база: %s не выполнено", getattr(fn, "__name__", fn))
        return default
