"""Тип района по данным: у центра, спальный, деловой, престижный; «тихо» — для конкретного дома.

Метки не назначаются руками — они считаются из признаков района (backend/geodata, OpenStreetMap)
и наших цен (медиана цены м² по объявлениям за 120 дней относительно города):

  у центра  — в районе центр города, или центр района ближе max(2 км, 20 % радиуса города);
  спальный  — дальше 40 % радиуса, мало офисов и магазинов (ниже 60-го перцентиля города),
              много школ и детсадов на км² (выше 30-го), цена не выше средней по городу более чем на 15 %;
  деловой   — офисов и магазинов на км² больше, чем у 80 % районов, и не дальше половины радиуса;
  престижный — цена м² не ниже 1,25 средней по городу (не меньше 8 объявлений в районе);
  тихо (дом) — крупная дорога не ближе 200 м и железная дорога не ближе 300 м.

Это оценка, а не факт: в интерфейсе подписывается «оценка по ценам и застройке». Пороги — в THRESHOLDS,
ручные поправки — в OVERRIDES (город -> район -> метки).
"""
from __future__ import annotations

import statistics
import time

from .. import db, geodata

THRESHOLDS = {"center_rel": 0.2, "center_km": 2.0, "sleeping_rel": 0.4, "sleeping_business_pct": 0.6,
              "sleeping_family_pct": 0.3, "sleeping_price": 1.15, "business_pct": 0.8, "business_rel": 0.5,
              "prestige_price": 1.25, "prestige_min_count": 8, "quiet_road_m": 200, "quiet_rail_m": 300}
OVERRIDES: dict[str, dict[str, list[str]]] = {}  # {"Москва": {"Новомосковский": ["-sleeping"]}}
LABELS = {"center": "у центра", "sleeping": "спальный", "business": "деловой", "prestige": "престижный", "quiet": "тихо"}
PRICE_TTL_S = 24 * 3600

_prices: dict[str, tuple[float, dict]] = {}


def price_stats(city: str) -> dict:
    """{'city': медиана ₽/м², 'districts': {район: (медиана ₽/м², объявлений)}} по нашей базе; кеш на сутки."""
    hit = _prices.get(city)
    if hit and time.time() - hit[0] < PRICE_TTL_S:
        return hit[1]
    from ..db import repo
    rows = db.safe(repo.listing_prices, city, default=[]) or []
    per: dict[str, list[float]] = {}
    all_ppm: list[float] = []
    for district, price, area in rows:
        if not area or not price or area < 15:
            continue
        ppm = price / area
        all_ppm.append(ppm)
        if district:
            per.setdefault(_key(district), []).append(ppm)
    stats = {"city": statistics.median(all_ppm) if all_ppm else None,
             "districts": {k: (statistics.median(v), len(v)) for k, v in per.items()}}
    _prices[city] = (time.time(), stats)
    return stats


def _key(name: str) -> str:
    return geodata.norm(name)


def _pct(values: list[float], v: float) -> float:
    """Доля районов города со значением не больше v (перцентиль от 0 до 1)."""
    return sum(1 for x in values if x <= v) / len(values) if values else 0.0


def labels(city: str) -> dict[str, dict]:
    """Метки всех районов города: {район: {'labels': [...], 'why': {...}}}. Пусто — нет геоданных."""
    c = geodata.city(city)
    if not c:
        return {}
    t = THRESHOLDS
    radius = max(c["radius_km"], 3.0)
    business = [d["features"]["business_per_km2"] for d in c["districts"]]
    family = [d["features"]["family_per_km2"] for d in c["districts"]]
    prices = price_stats(city)
    out = {}
    for d in c["districts"]:
        rel = d["dist_km"] / radius
        f = d["features"]
        b_pct, f_pct = _pct(business, f["business_per_km2"]), _pct(family, f["family_per_km2"])
        median, count = prices["districts"].get(_key(d["name"]), (None, 0))
        ratio = round(median / prices["city"], 2) if median and prices["city"] and count >= 3 else None
        got = []
        if d.get("has_center") or d["dist_km"] <= max(t["center_km"], t["center_rel"] * radius):
            got.append("center")
        if (rel >= t["sleeping_rel"] and b_pct <= t["sleeping_business_pct"] and f_pct >= t["sleeping_family_pct"]
                and (ratio is None or ratio <= t["sleeping_price"])):
            got.append("sleeping")
        if b_pct >= t["business_pct"] and rel <= t["business_rel"]:
            got.append("business")
        if ratio is not None and ratio >= t["prestige_price"] and count >= t["prestige_min_count"]:
            got.append("prestige")
        for change in (OVERRIDES.get(city) or {}).get(d["name"], []):
            if change.startswith("-"):
                got = [x for x in got if x != change[1:]]
            elif change not in got:
                got.append(change)
        out[d["name"]] = {"labels": got, "why": {"dist_km": d["dist_km"], "radius_km": radius,
                                                 "business_pct": round(b_pct, 2), "family_pct": round(f_pct, 2),
                                                 "price_ratio": ratio, "price_count": count}}
    _fallbacks(out)
    return out


def _fallbacks(out: dict) -> None:
    """В городах с 4–8 районами процентили грубые: бывает, что ни один район не прошёл порог.
    Тогда «деловой» — самый плотный по офисам и магазинам район у центра, «спальные» — до двух
    удалённых районов с наибольшей долей школ и детсадов. Ручные OVERRIDES важнее."""
    t = THRESHOLDS
    rows = list(out.items())
    if rows and not any("business" in v["labels"] for _, v in rows):
        near = [r for r in rows if "center" in r[1]["labels"]] or [r for r in rows if r[1]["why"]["dist_km"] <= t["business_rel"] * r[1]["why"]["radius_km"]]
        if near:
            max(near, key=lambda r: r[1]["why"]["business_pct"])[1]["labels"].append("business")
    if rows and not any("sleeping" in v["labels"] for _, v in rows):
        far = [r for r in rows if "center" not in r[1]["labels"] and "business" not in r[1]["labels"]
               and r[1]["why"]["dist_km"] >= t["sleeping_rel"] * r[1]["why"]["radius_km"]]
        for _, v in sorted(far, key=lambda r: -r[1]["why"]["family_pct"])[:2]:
            v["labels"].append("sleeping")


def for_type(city: str, kind: str, limit: int = 12) -> list[str]:
    """Районы города с меткой kind, самые подходящие — первыми."""
    rows = [(name, v) for name, v in labels(city).items() if kind in v["labels"]]
    order = {
        "sleeping": lambda r: (-r[1]["why"]["family_pct"], r[1]["why"]["business_pct"]),
        "prestige": lambda r: -(r[1]["why"]["price_ratio"] or 0),
        "center": lambda r: r[1]["why"]["dist_km"],
        "business": lambda r: -r[1]["why"]["business_pct"],
    }.get(kind, lambda r: r[0])
    return [name for name, _ in sorted(rows, key=order)][:limit]


def of_listing(x: dict) -> list[str] | None:
    """Метки района квартиры; None — нет геоданных или район не определён."""
    table = labels(x.get("city"))
    if not table:
        return None
    name = x.get("district")
    if name:
        key = _key(name)
        for district, v in table.items():
            if _key(district) == key or _key(district).startswith(key[:6]) or key.startswith(_key(district)[:6]):
                return v["labels"]
    d = geodata.district_of(x.get("city"), x.get("lat"), x.get("lng"))
    return table[d["name"]]["labels"] if d else None


def quiet(x: dict) -> dict | None:
    """Шум у дома: {'road_m', 'rail_m', 'quiet'}; None — нет геоданных города."""
    if not geodata.extra(x.get("city")) or x.get("lat") is None or x.get("lng") is None:
        return None
    t = THRESHOLDS
    road = geodata.nearest_line_m(x["city"], "major", x["lat"], x["lng"], 1000)
    rail = geodata.nearest_line_m(x["city"], "rail", x["lat"], x["lng"], 1000)
    road = 1000 if road is None else road  # ничего в 1 км — тихо
    rail = 1000 if rail is None else rail
    return {"road_m": road, "rail_m": rail, "quiet": road >= t["quiet_road_m"] and rail >= t["quiet_rail_m"]}


def describe(city: str, kind: str, names: list[str]) -> str:
    """Фраза для клиента: какие районы и почему (честно — это оценка)."""
    label = {"sleeping": "Спальные районы", "prestige": "Престижные районы", "center": "Районы у центра",
             "business": "Деловые районы", "prestige_fallback": "Престижные районы"}.get(kind, "Районы")
    basis = {"sleeping": "по застройке, удалённости от центра и ценам",
             "prestige": "по цене м² относительно города", "center": "по расстоянию до центра",
             "business": "по плотности офисов и магазинов по OpenStreetMap",
             "prestige_fallback": "цен по городу пока мало, поэтому беру центральные и деловые районы"}.get(kind, "по данным")
    shown = ", ".join(names[:8]) + (f" и ещё {len(names) - 8}" if len(names) > 8 else "")
    return f"{label} — {city} (оценка {basis}): {shown}. Ищу в них — если какой-то не подходит, напишите."
