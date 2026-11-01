"""Источники объявлений. Агент вызывает collect() и не знает деталей площадок.

Сейчас подключён Циан: живой запрос -> кеш сессии -> сохранённый срез
(тоже реальные объявления Циана). Синтетики нет: если данных нет, агент так и говорит.
Всё, что пришло с площадки вживую, запоминается в базе агента вместе с признаками (store.py).
"""
from __future__ import annotations

from . import cian, store
from .base import SourceUnavailable, description_flags, normalize_city

__all__ = ["collect", "more", "stored", "verify", "markets", "comparables", "market", "normalize_city", "description_flags",
           "SourceUnavailable", "cian", "store", "SUPPORTED_CITIES", "FEATURED_CITIES"]


SUPPORTED_CITIES = list(cian.REGIONS)
FEATURED_CITIES = cian.FEATURED_CITIES  # проверены на живом Циане — их предлагаем кнопками


def markets(profile: dict) -> list[bool]:
    """Где искать: [False] — вторичка, [True] — новостройки, оба — если клиент выбрал «и там, и там»."""
    return {"new": [True], "both": [False, True]}.get(profile.get("housing"), [False])


async def collect(profile: dict) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по рынкам): вторичка и/или новостройки — по выбору клиента."""
    items: list[dict] = []
    report: list[dict] = []
    for new in markets(profile):
        label = cian.SOURCE_NAME + (" · новостройки" if new else " · вторичка" if profile.get("housing") == "both" else "")
        try:
            got, rep = await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1, profile.get("okrug"), new)
        except SourceUnavailable as exc:
            report.append({"source": label, "count": 0, "status": "unavailable", "note": str(exc), "new": new})
            continue
        if rep["status"] == "live":
            store.remember(got)
        store.track(profile, new)
        got = store.apply_corrections(cian.drop_outliers(got))
        items += got
        report.append({"source": label, "count": len(got), **rep, "new": new})
    return items, report


async def market(profile: dict) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    return await cian.market(profile["city"], profile["rooms"], profile.get("okrug"), profile.get("housing") == "new")


def comparables(profile: dict) -> list[dict]:
    """Все известные объявления того же города и комнатности (срез + база агента за месяц) — основа справедливой цены."""
    if not cian.supported(profile.get("city")):
        return []
    known: dict[str, dict] = {}
    for new in markets(profile):
        for x in cian.load_snapshot(profile["city"], profile["rooms"], new)[0]:
            known[x["id"]] = x
        for x in store.known(profile["city"], profile["rooms"], new=new):
            known.setdefault(x["id"], x)
    return list(known.values())


async def more(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи (если площадка отвечает)."""
    out: list[dict] = []
    for new in markets(profile):
        try:
            items, rep = await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], page, profile.get("okrug"), new)
        except SourceUnavailable:
            continue
        if rep["status"] == "live":
            store.remember(items)
        out += items
    return store.apply_corrections(out)


def stored(profile: dict, exclude: set[str] = frozenset()) -> list[dict]:
    """Объявления из базы агента по запросу — по выбранным рынкам."""
    return [x for new in markets(profile) for x in store.stored(profile, exclude, new=new)]


async def verify(listing: dict) -> dict:
    """Открыть страницу объявления: не сняли ли его, и обновить параметры по свежей странице.
    -> {"alive": True | False | None, "note", "checked_at", "listing": свежее объявление или None}.
    Уточнения продавца из звонков остаются поверх свежих данных площадки."""
    from datetime import datetime, timezone
    page = await cian.check_listing_page(listing.get("url"))
    store.page_checked(listing["id"], page["alive"], page["note"])
    fresh = cian.normalize(page["offer"], listing["city"], new=bool(listing.get("is_new"))) if page["offer"] else None
    if fresh and fresh["id"] == listing["id"]:
        # страница объявления отдаёт не все поля (ссылки, иногда адреса нет) — пустое не затирает известное
        known = {k: v for k, v in listing.items() if k not in ("listed", "confirmed", "stored_at", "nearby")}
        fresh = {**known, **{k: v for k, v in fresh.items() if v not in (None, "", [])}}
        store.remember([fresh])
        store.apply_corrections([fresh])
    else:
        fresh = None
    return {"alive": page["alive"], "note": page["note"], "listing": fresh,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
