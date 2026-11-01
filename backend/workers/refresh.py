"""Ночной обход: держит базу объявлений свежей по тем поискам, которые задавали клиенты.

Для каждого поиска за последний месяц (tracked_searches):
  1. первые REFRESH_PAGES страниц выдачи Циана -> база: новые объявления, изменения цены (история);
  2. объявления этого поиска, которых не было в выдаче, — открываем страницу (не больше REFRESH_CHECK_LIMIT за обход):
     сняли — помечаем removed, живо — обновляем параметры по странице (площадь, кухня, цена, описание…).
Уточнения продавца из звонков (source = call) обход не трогает.
Если выдачу получить не удалось (капча, сеть), пропавшие не проверяем: неполный обход ничего не снимает.
Каждый обход — строка crawl_runs: по ней видно, жив ли сервис и что он нашёл.

Запуск: .venv/bin/python -m backend.workers.refresh --once      — один обход сейчас
        REFRESH_AT=04:00 в .env                                   — каждую ночь внутри сервера
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta

from .. import db
from ..config import settings
from ..db import repo
from ..db.models import utcnow
from .. import sources
from ..sources import SourceUnavailable, cian, store

log = logging.getLogger(__name__)


async def run_once(pages: int | None = None, check_limit: int | None = None) -> dict:
    pages = pages or settings.refresh_pages
    budget = settings.refresh_check_limit if check_limit is None else check_limit
    started = utcnow()
    run_id = db.safe(repo.start_crawl)
    stats = {"searches": 0, "seen": 0, "added": 0, "price_changed": 0, "removed": 0}
    errors: list[str] = []
    for t in db.safe(repo.tracked_searches, default=[]) or []:
        stats["searches"] += 1
        complete = True
        for page in range(1, pages + 1):
            try:
                items, rep = await cian.search(t.city, t.rooms, t.budget_max, page, t.okrug or None, t.is_new)
            except SourceUnavailable as exc:
                complete = False
                errors.append(f"{t.city}, {t.rooms}-комн., стр. {page}: {exc}")
                break
            if rep["status"] not in ("live", "cached"):  # сохранённый срез — не свежая выдача
                complete = False
                break
            counts = store.remember(items) or {}
            for k in ("seen", "added", "price_changed"):
                stats[k] += counts.get(k, 0)
            if len(items) < 20:  # последняя страница выдачи
                break
        if not complete:
            continue
        db.safe(repo.mark_crawled, t.id)
        if budget <= 0:
            continue
        for x in db.safe(repo.missing_in_scope, t, started, budget, default=[]) or []:
            budget -= 1
            page = await sources.verify(x)  # снято ли, а если нет — обновить параметры по странице
            if page["alive"] is False:
                stats["removed"] += 1
            elif page["alive"]:
                db.safe(repo.mark_seen, x["id"])
    status = "ok" if not errors else ("partial" if stats["seen"] else "failed")
    if run_id is not None:
        db.safe(repo.finish_crawl, run_id, status, stats, errors)
    log.info("обход объявлений: %s, %s", status, stats)
    return {"status": status, **stats, "errors": errors}


def _seconds_until(at: str, now: datetime | None = None) -> float:
    """Сколько ждать до ближайшего «ЧЧ:ММ» по местному времени сервера."""
    now = now or datetime.now()
    h, m = (int(x) for x in at.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily(at: str) -> None:
    """Каждый день в at (ЧЧ:ММ). Ошибка одного обхода не останавливает следующие."""
    while True:
        await asyncio.sleep(_seconds_until(at))
        try:
            await run_once()
        except Exception:  # noqa: BLE001 — обход фоновый, сервер должен жить
            log.exception("обход объявлений упал")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if "--once" not in sys.argv:
        print("Запуск: python -m backend.workers.refresh --once (или REFRESH_AT=04:00 в .env — каждую ночь в сервере)")
        sys.exit(0)
    db.configure()
    print(asyncio.run(run_once()))
