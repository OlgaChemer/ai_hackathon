"""База объявлений агента: всё, что агент видел на площадках, с признаками.

Признаки считаются один раз и нужны всем, кто ищет похожее:
  поле площадки или описание  — балкон, лоджия, санузел, гардеробная… (при каждом обновлении объявления)
  карта (geo.py)              — что в 1 км: школа, поликлиника, парк, вода… (раз в POI_TTL_DAYS)
  звонок продавцу             — уточнения: «кухня 18 м², а не 23», «балкон есть» (source = call)
Поэтому агент сначала смотрит в базу, а на площадку идёт за свежим.
Данные площадки хранятся как есть и обновляются; слова продавца хранятся отдельно и при чтении накладываются
поверх (apply_corrections) — ни обход, ни новый поиск их не перезаписывают.
Объявление из базы перед звонком проверяется: не снято ли (sources.verify).
"""
from __future__ import annotations

from datetime import datetime

from .. import db
from ..db import repo

SEEN_DAYS = 7  # объявления, которые агент видел на площадке за неделю, — ещё актуальны для подбора

# параметры квартиры, которые продавец может уточнить в звонке: поле объявления -> тип
FIELDS = {"kitchen": float, "area": float, "floor": int, "floors": int, "year": int, "rooms": int, "condition": str}


def _features(x: dict) -> list[tuple[str, str, object, str]]:
    from ..agent.features import FEATURES, feature_status  # агент использует источники, а не наоборот: импорт по месту

    has_counts = {"balcony": x.get("balconies") is not None, "balcony_loggia": x.get("loggias") is not None,
                  "separate_wc": x.get("separate_wc") is not None}
    return [(x["id"], key, feature_status(x, key), "cian" if has_counts.get(key) else "text") for key in FEATURES]


def remember(items: list[dict]) -> dict | None:
    """Живая выдача площадки -> база: объявления, история цены, признаки из полей и описания."""
    if not items:
        return None

    def save(s):
        counts = repo.upsert_listings(s, items)
        repo.save_features(s, [f for x in items for f in _features(x)])
        return counts
    return db.safe(save)


def stored(profile: dict, exclude: set[str] = frozenset(), new: bool = False) -> list[dict]:
    """Объявления из базы по запросу клиента (город, комнаты, бюджет, округ), которых нет в exclude.
    У каждого — stored_at: когда агент последний раз видел его на площадке."""
    rows = db.safe(repo.find_listings, profile["city"], profile["rooms"], profile.get("budget_max"),
                   profile.get("okrug"), is_new=new, seen_days=SEEN_DAYS, exclude=set(exclude), default=[]) or []
    return apply_corrections([{**data, "stored_at": _iso(seen)} for data, seen in rows])


def known(city: str, rooms: int, days: float = 30, new: bool = False) -> list[dict]:
    """Все объявления города и комнатности, которые агент видел за days дней (без бюджета) — аналоги для цены."""
    rows = db.safe(repo.find_listings, city, rooms, None, is_new=new, seen_days=days, default=[]) or []
    return [data for data, _ in rows]


def correct(listing_id: str, corrections: dict) -> None:
    """Уточнения продавца из звонка -> база (source = call): поля — field:<имя>, удобства — их ключ (balcony…)."""
    rows = [(listing_id, f"field:{k}" if k in FIELDS else k, v, "call") for k, v in corrections.items()]
    db.safe(repo.save_features, rows)


def apply_corrections(items: list[dict]) -> list[dict]:
    """Накладывает уточнения продавца на объявления: x["kitchen"] = 18, в x["listed"] — что было в объявлении,
    удобства — в x["confirmed"] (их проверяет tools.feature_status раньше описания)."""
    known = db.safe(repo.load_corrections, [x["id"] for x in items], default={}) or {}
    for x in items:
        for key, value in (known.get(x["id"]) or {}).items():
            if key.startswith("field:"):
                name = key[6:]
                if x.get(name) != value:
                    x.setdefault("listed", {})[name] = x.get(name)
                    x[name] = value
            else:
                x.setdefault("confirmed", {})[key] = value
    return items


def track(profile: dict, new: bool) -> None:
    """Запомнить поиск: ночной обход (workers/refresh.py) будет держать базу по нему свежей."""
    db.safe(repo.track_search, profile["city"], profile["rooms"], new, profile.get("okrug"), profile.get("budget_max"))


def page_checked(listing_id: str, alive: bool | None, note: str) -> None:
    db.safe(repo.save_page_check, listing_id, alive, note)


def stats(profile: dict) -> dict:
    """Для экрана поиска: сколько квартир по запросу в базе, у скольких проверено «что рядом», последний обход."""
    out = db.safe(repo.listing_stats, profile["city"], profile["rooms"]) or {"total": 0, "mapped": 0}
    out["crawl"] = db.safe(repo.last_crawl)
    return out


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="minutes") + "Z"
