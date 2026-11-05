"""Источники объявлений. Агент вызывает collect() и не знает деталей площадок.

Сейчас подключён Циан: живой запрос -> кеш сессии -> сохранённый срез
(тоже реальные объявления Циана). Синтетики нет: если данных нет, агент так и говорит.
Всё, что пришло с площадки вживую, запоминается в базе агента вместе с признаками (store.py).
"""
from __future__ import annotations
from datetime import datetime,timezone
import asyncio
import time
from ..config import settings

from . import cian, store
from .base import SourceUnavailable, description_flags, normalize_city

__all__ = ["collect", "more", "stored", "verify", "markets", "comparables", "market", "normalize_city", "description_flags",
           "SourceUnavailable", "cian", "store", "SUPPORTED_CITIES", "FEATURED_CITIES"]


SUPPORTED_CITIES = list(cian.REGIONS)
FEATURED_CITIES = cian.FEATURED_CITIES  # проверены на живом Циане — их предлагаем кнопками


class PageBatch(list):
    def __init__(self,items=(),reports=()):super().__init__(items);self.reports=list(reports)


def provenance(items,rep):
    return [{**x,'provenance':{'source':'cian','status':rep.get('status'),
                              'observed_at':rep.get('fetched_at'),'url':x.get('url')}} for x in items]


def markets(profile: dict) -> list[bool]:
    """Где искать: [False] — вторичка, [True] — новостройки, оба — если клиент выбрал «и там, и там»."""
    return {None: [False, True], "new": [True], "both": [False, True]}.get(profile.get("housing"), [False])


async def _collect_one(profile: dict, on_batch=None) -> tuple[list[dict], list[dict]]:
    """Возвращает (объявления, отчёт по рынкам): вторичка и/или новостройки — по выбору клиента."""
    items: list[dict] = []
    report: list[dict] = []
    for new in markets(profile):
        label = cian.SOURCE_NAME + (" · новостройки" if new else " · вторичка" if profile.get("housing") == "both" else "")
        try:
            remaining=profile.get('_source_deadline',time.monotonic()+settings.search_budget_s)-time.monotonic()
            if remaining<=0:raise asyncio.TimeoutError()
            got, rep = await asyncio.wait_for(cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], 1, profile.get("okrug"), new),remaining)
        except asyncio.TimeoutError:
            got,stamp=cian.load_snapshot(profile['city'],profile['rooms'],new)
            if not got:
                report.append({'source':label,'status':'unavailable','count':0,'note':'Бюджет поиска исчерпан',
                               'reason':'search_budget_exhausted','new':new,'page':1})
                continue
            rep={'status':'snapshot','fetched_at':stamp,'reason':'search_budget_exhausted','note':'Бюджет поиска исчерпан; использован сохранённый срез'}
        except SourceUnavailable as exc:
            report.append({"source": label, "count": 0, "status": "unavailable", "note": str(exc), "new": new})
            continue
        if rep["status"] == "live":
            store.remember(got)
        store.track(profile, new)
        got = store.apply_corrections(provenance(cian.drop_outliers(got),rep))
        items += got
        report.append({"source": label, "count": len(got), **rep, "new": new,'page':1})
        if on_batch:on_batch(got)
    return items, report


async def market(profile: dict, on_progress=None) -> dict | None:
    """Цены рынка по запросу (без бюджета) — чтобы клиенту было от чего оттолкнуться."""
    if not cian.supported(profile.get("city")) or profile.get("rooms") is None:
        return None
    from .market_estimate import estimate
    return await estimate(profile,on_progress)


def _comparables_one(profile: dict) -> list[dict]:
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


async def _more_one(profile: dict, page: int) -> list[dict]:
    """Следующая страница живой выдачи (если площадка отвечает)."""
    out: list[dict] = []
    reports=[]
    for new in markets(profile):
        try:
            remaining=profile.get('_source_deadline',time.monotonic()+settings.search_budget_s)-time.monotonic()
            if remaining<=0:raise asyncio.TimeoutError()
            items, rep = await asyncio.wait_for(cian.search(profile.get("city"), profile["rooms"], profile["budget_max"], page, profile.get("okrug"), new),remaining)
        except asyncio.TimeoutError:
            reports.append({'source':cian.SOURCE_NAME,'status':'unavailable','count':0,'note':'Бюджет поиска исчерпан',
                            'reason':'search_budget_exhausted','city':profile['city'],'rooms':profile['rooms'],'new':new,'page':page})
            continue
        except SourceUnavailable as exc:
            reports.append({'source':cian.SOURCE_NAME,'status':'unavailable','note':str(exc),'count':0,
                            'city':profile['city'],'rooms':profile['rooms'],'new':new,'page':page})
            continue
        if rep["status"] == "live":
            store.remember(items)
        reports.append({'source':cian.SOURCE_NAME,**rep,'count':len(items),'city':profile['city'],
                        'rooms':profile['rooms'],'new':new,'page':page})
        out += provenance(items,rep)
    return PageBatch(store.apply_corrections(out),reports)


def _stored_one(profile: dict, exclude: set[str] = frozenset()) -> list[dict]:
    """Объявления из базы агента по запросу — по выбранным рынкам."""
    return [x for new in markets(profile) for x in store.stored(profile, exclude, new=new)]


def queries(profile: dict):
    """Each explicitly accepted city/room variant gets its own Cian and DB query."""
    for city in dict.fromkeys([profile.get("city"), *(profile.get("cities") or [])]):
        for rooms in profile.get("rooms_options") or [profile.get("rooms")]:
            yield {**profile, "city": city, "rooms": rooms}


def unique(items):
    return list({x["id"]: x for x in items}.values())


async def collect(profile: dict, on_batch=None):
    items, reports = [], []
    deadline=profile.get('_source_deadline',time.monotonic()+settings.search_budget_s)
    for q in queries({**profile,'_source_deadline':deadline}):
        got, report = await _collect_one(q,on_batch=on_batch) if on_batch else await _collect_one(q)
        items.extend(got)
        reports.extend({**r, "city": q["city"], "rooms": q["rooms"]} for r in report)
    from ..telemetry import dependency
    dependency('listings','available' if any(r.get('status')=='live' for r in reports) else 'cached_only' if items else 'degraded')
    return unique(items), reports


async def more(profile: dict, page: int):
    items,reports = [],[]
    deadline=profile.get('_source_deadline',time.monotonic()+settings.search_budget_s)
    for q in queries({**profile,'_source_deadline':deadline}):
        batch=await _more_one(q,page);items.extend(batch);reports.extend(getattr(batch,'reports',[]))
    return PageBatch(unique(items),reports)


def stored(profile: dict, exclude: set[str] = frozenset()):
    return unique([x for q in queries(profile) for x in _stored_one(q, exclude)])


def comparables(profile: dict):
    return unique([x for q in queries(profile) for x in _comparables_one(q)])


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
