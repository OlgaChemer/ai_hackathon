"""Источники объявлений. Агент вызывает collect() и не знает деталей площадок.

Сейчас подключён Циан: живой запрос -> кеш сессии -> сохранённый срез
(тоже реальные объявления Циана). Синтетики нет: если данных нет, агент так и говорит.
"""
from __future__ import annotations

from . import cian, places
from .base import SourceUnavailable, description_flags, normalize_city

__all__ = ["collect", "more", "comparables", "market", "normalize_city", "description_flags", "SourceUnavailable", "cian", "SUPPORTED_CITIES"]

SUPPORTED_CITIES = list(cian.REGIONS)


async def collect(profile: dict) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по площадкам)."""
    try:
        items, rep = await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1, profile.get("okrug"))
        items = cian.drop_outliers(items)
        return items, [{"source": cian.SOURCE_NAME, "count": len(items), **rep}]
    except SourceUnavailable as exc:
        return [], [{"source": cian.SOURCE_NAME, "count": 0, "status": "unavailable", "note": str(exc)}]


async def market(profile: dict) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    return await cian.market(profile["city"], profile["rooms"], profile.get("okrug"))


def comparables(profile: dict) -> list[dict]:
    """Все сохранённые объявления того же города и комнатности — база для справедливой цены."""
    if not cian.supported(profile.get("city")):
        return []
    saved, _ = cian.load_snapshot(profile["city"], profile["rooms"])
    return saved


async def more(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи (если площадка отвечает)."""
    try:
        items, _ = await cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], page, profile.get("okrug"))
        return items
    except SourceUnavailable:
        return []
