"""Запросы к базе. Все функции принимают сессию первым аргументом: repo.fn(db, ...)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .models import (Chat, ChatEvent, CrawlRun, Listing, ListingFeature, ListingPrice, SecurityEvent, TrackedSearch,
                     Visitor, utcnow)

# ================================================================ посетители и диалоги


def ensure_visitor(db: Session, vid: str | None) -> None:
    if vid and db.get(Visitor, vid) is None:
        db.add(Visitor(id=vid))
        db.flush()


def claim_visitor(db: Session, vid: str | None, user_id: int) -> int:
    """Вход: браузер и его диалоги переходят пользователю. Возвращает, сколько диалогов перешло."""
    if not vid:
        return 0
    ensure_visitor(db, vid)
    db.get(Visitor, vid).user_id = user_id
    res = db.execute(update(Chat).where(Chat.visitor_id == vid, Chat.user_id.is_(None)).values(user_id=user_id))
    return res.rowcount or 0


def create_chat(db: Session, chat_id: str, title: str, vid: str | None, user_id: int | None) -> None:
    ensure_visitor(db, vid)
    db.add(Chat(id=chat_id, title=title[:120], visitor_id=vid, user_id=user_id))


def get_chat(db: Session, chat_id: str) -> Chat | None:
    return db.get(Chat, chat_id)


def can_open(chat: Chat | None, vid: str | None, user_id: int | None) -> bool:
    """Диалог открывает браузер, где он начат, и пользователь, к которому он привязан (с любого устройства)."""
    if chat is None:
        return False
    return bool((vid and chat.visitor_id == vid) or (user_id is not None and chat.user_id == user_id))


def save_event(db: Session, chat_id: str, seq: int, type_: str, payload: dict) -> None:
    db.add(ChatEvent(chat_id=chat_id, seq=seq, type=type_, payload=payload))
    db.execute(update(Chat).where(Chat.id == chat_id).values(last_seq=seq, updated_at=utcnow()))


def update_chat(db: Session, chat_id: str, **fields: Any) -> None:
    db.execute(update(Chat).where(Chat.id == chat_id).values(**fields))


def chat_events(db: Session, chat_id: str, after: int = 0) -> list[dict]:
    rows = db.scalars(select(ChatEvent).where(ChatEvent.chat_id == chat_id, ChatEvent.seq > after)
                      .order_by(ChatEvent.seq))
    return [r.payload for r in rows]


def list_chats(db: Session, vid: str | None, user_id: int | None, limit: int = 50) -> list[dict]:
    """Диалоги этого браузера и пользователя — новые сверху."""
    owners = []
    if vid:
        owners.append(Chat.visitor_id == vid)
    if user_id is not None:
        owners.append(Chat.user_id == user_id)
    if not owners:
        return []
    rows = db.scalars(select(Chat).where(or_(*owners)).order_by(Chat.updated_at.desc()).limit(limit))
    return [{"id": c.id, "title": c.title, "status": c.status, "updated_at": _iso(c.updated_at)} for c in rows]


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() + "Z" if t else None


# ================================================================ квартиры

def upsert_listings(db: Session, items: list[dict]) -> dict:
    """Выдача площадки -> база. Новое — добавить; цена изменилась — строка в историю. Возвращает счётчики."""
    now = utcnow()
    added = changed = 0
    existing = {x.id: x for x in db.scalars(select(Listing).where(Listing.id.in_([i["id"] for i in items])))}
    for x in items:
        row = existing.get(x["id"])
        if row is None:
            db.add(Listing(id=x["id"], source=x.get("source") or "cian", city=x["city"], rooms=x.get("rooms") or 0,
                           is_new=bool(x.get("is_new")), price=x["price"], okrug=x.get("okrug"), data=x,
                           first_seen_at=now, last_seen_at=now))
            db.add(ListingPrice(listing_id=x["id"], price=x["price"], observed_at=now))
            added += 1
            continue
        if row.price != x["price"]:
            db.add(ListingPrice(listing_id=x["id"], price=x["price"], observed_at=now))
            changed += 1
        row.price, row.data, row.okrug, row.last_seen_at = x["price"], x, x.get("okrug"), now
        row.status, row.removed_at = "active", None
    db.flush()
    return {"added": added, "price_changed": changed, "seen": len(items)}


def save_features(db: Session, rows: list[tuple[str, str, Any, str]]) -> int:
    """[(listing_id, key, value, source)] — записать или обновить. Признаки неизвестных объявлений пропускаем.
    Сказанное продавцом (source = call) площадка и описание не перезаписывают — только новый звонок."""
    if not rows:
        return 0
    known = set(db.scalars(select(Listing.id).where(Listing.id.in_({r[0] for r in rows}))))
    now = utcnow()
    n = 0
    for listing_id, key, value, source in rows:
        if listing_id not in known:
            continue
        f = db.get(ListingFeature, (listing_id, key))
        if f is None:
            db.add(ListingFeature(listing_id=listing_id, key=key, value=value, source=source, observed_at=now))
        elif f.source == "call" and source != "call":
            continue
        else:
            f.value, f.source, f.observed_at = value, source, now
        n += 1
    return n


def load_features(db: Session, ids: list[str], prefix: str = "", max_age_days: float | None = None) -> dict[str, dict]:
    """{listing_id: {key: value}} — признаки с ключом на prefix, не старше max_age_days."""
    if not ids:
        return {}
    q = select(ListingFeature).where(ListingFeature.listing_id.in_(ids))
    if prefix:
        q = q.where(ListingFeature.key.startswith(prefix))
    if max_age_days is not None:
        q = q.where(ListingFeature.observed_at >= utcnow() - timedelta(days=max_age_days))
    out: dict[str, dict] = {}
    for f in db.scalars(q):
        out.setdefault(f.listing_id, {})[f.key] = f.value
    return out


def load_corrections(db: Session, ids: list[str]) -> dict[str, dict]:
    """Что продавец сказал в звонке: {listing_id: {key: value}} — поверх данных площадки."""
    if not ids:
        return {}
    out: dict[str, dict] = {}
    for f in db.scalars(select(ListingFeature).where(ListingFeature.listing_id.in_(ids), ListingFeature.source == "call")):
        out.setdefault(f.listing_id, {})[f.key] = f.value
    return out


def find_listings(db: Session, city: str, rooms: int, budget_max: int | None, okrug: str | None = None,
                  is_new: bool = False, seen_days: float = 7, exclude: set[str] | frozenset = frozenset(),
                  limit: int = 400) -> list[tuple[dict, datetime]]:
    """Объявления, которые агент видел на площадке за последние seen_days: [(объявление, когда видели)]."""
    q = select(Listing).where(Listing.city == city, Listing.rooms == rooms, Listing.is_new == is_new,
                              Listing.status == "active", Listing.last_seen_at >= utcnow() - timedelta(days=seen_days))
    if budget_max:
        q = q.where(Listing.price <= budget_max)
    if okrug:
        q = q.where(Listing.okrug == okrug)
    rows = db.scalars(q.order_by(Listing.last_seen_at.desc()).limit(limit))
    return [(r.data, r.last_seen_at) for r in rows if r.id not in exclude]


def listing_stats(db: Session, city: str, rooms: int) -> dict:
    """Сколько квартир по запросу в базе и у скольких уже проверено по карте, что рядом."""
    total = db.scalar(select(func.count()).select_from(Listing)
                      .where(Listing.city == city, Listing.rooms == rooms, Listing.status == "active")) or 0
    mapped = db.scalar(select(func.count(func.distinct(ListingFeature.listing_id))).select_from(ListingFeature)
                       .join(Listing, Listing.id == ListingFeature.listing_id)
                       .where(Listing.city == city, Listing.rooms == rooms, ListingFeature.source == "map")) or 0
    return {"total": total, "mapped": mapped}


def save_page_check(db: Session, listing_id: str, alive: bool | None, note: str) -> None:
    """Результат проверки страницы объявления. Снято — больше не попадает в подборки из базы."""
    row = db.get(Listing, listing_id)
    if row is None:
        return
    row.page_checked_at, row.page_alive, row.page_note = utcnow(), alive, note[:160]
    if alive is False:
        row.status, row.removed_at = "removed", utcnow()



# ================================================================ обход площадки


def track_search(db: Session, city: str, rooms: int, is_new: bool, okrug: str | None, budget_max: int | None) -> None:
    """Запомнить поиск клиента: ночной обход будет обновлять базу по нему (бюджет — самый большой из запрошенных)."""
    t = db.scalar(select(TrackedSearch).where(TrackedSearch.city == city, TrackedSearch.rooms == rooms,
                                              TrackedSearch.is_new == is_new, TrackedSearch.okrug == (okrug or "")))
    if t is None:
        t = TrackedSearch(city=city, rooms=rooms, is_new=is_new, okrug=okrug or "", budget_max=budget_max, requested_count=0)
        db.add(t)
    elif budget_max and (t.budget_max is None or budget_max > t.budget_max):
        t.budget_max = budget_max
    t.requested_count = (t.requested_count or 0) + 1
    t.last_requested_at = utcnow()


def tracked_searches(db: Session, days: float = 30) -> list[TrackedSearch]:
    return list(db.scalars(select(TrackedSearch).where(TrackedSearch.last_requested_at >= utcnow() - timedelta(days=days))
                           .order_by(TrackedSearch.requested_count.desc())))


def missing_in_scope(db: Session, t: TrackedSearch, since: datetime, limit: int) -> list[dict]:
    """Объявления этого поиска, которых не было в выдаче с начала обхода, — кандидаты на «снято»."""
    q = select(Listing).where(Listing.city == t.city, Listing.rooms == t.rooms, Listing.is_new == t.is_new,
                              Listing.status == "active", Listing.last_seen_at < since)
    if t.okrug:
        q = q.where(Listing.okrug == t.okrug)
    if t.budget_max:
        q = q.where(Listing.price <= int(t.budget_max * 1.05))
    return [r.data for r in db.scalars(q.order_by(Listing.last_seen_at).limit(limit))]


def mark_seen(db: Session, listing_id: str) -> None:
    """Страница открылась — объявление живо, хотя в первые страницы выдачи не попало."""
    row = db.get(Listing, listing_id)
    if row is not None:
        row.last_seen_at = utcnow()


def last_crawl(db: Session) -> dict | None:
    r = db.scalar(select(CrawlRun).where(CrawlRun.finished_at.is_not(None)).order_by(CrawlRun.id.desc()).limit(1))
    if r is None:
        return None
    return {"finished_at": _iso(r.finished_at), "status": r.status, "seen": r.seen, "added": r.added,
            "price_changed": r.price_changed, "removed": r.removed}


def start_crawl(db: Session) -> int:
    r = CrawlRun(started_at=utcnow())
    db.add(r)
    db.flush()
    return r.id


def finish_crawl(db: Session, run_id: int, status: str, stats: dict, errors: list[str]) -> None:
    r = db.get(CrawlRun, run_id)
    if r is not None:
        r.finished_at, r.status, r.errors = utcnow(), status, errors[:20] or None
        for k, v in stats.items():
            setattr(r, k, v)


def mark_crawled(db: Session, search_id: int) -> None:
    t = db.get(TrackedSearch, search_id)
    if t is not None:
        t.last_crawled_at = utcnow()


# ================================================================ безопасность


def log_security(db: Session, chat_id: str | None, visitor_id: str | None, user_id: int | None, where: str,
                 category: str, sample: str) -> None:
    db.add(SecurityEvent(chat_id=chat_id, visitor_id=visitor_id, user_id=user_id, where=where, category=category,
                         sample=sample))


def security_events(db: Session, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(limit))
    return [{"at": r.created_at.isoformat(timespec="seconds"), "chat": r.chat_id, "where": r.where, "category": r.category,
             "sample": r.sample} for r in rows]
