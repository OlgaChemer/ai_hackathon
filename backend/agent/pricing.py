"""Справедливая цена по аналогам, аргументы для торга и оценка квартиры для подборки.

Числа считает код, а не модель: результат воспроизводим и объясним, модель не может «придумать» цену.
"""
from __future__ import annotations

import math
import statistics

from .numbers import fmt, plural
from .profile import in_districts


BARGAIN_DISCOUNT = 0.97  # в объявлениях цена выше, чем в сделке: ~3% (по данным рынка о торге)


def condition_factor(c: str | None) -> float:
    return {
        "дизайнерский": 1.08, "евроремонт": 1.06, "косметика": 1.0, "чистовая отделка": 0.98,
        "предчистовая": 0.94, "без отделки": 0.9, "требует ремонта": 0.92,
    }.get(c or "", 1.0)


def year_factor(y: int | None) -> float:
    if not y:
        return 1.0
    return 1.03 if y >= 2015 else 1.0 if y >= 2000 else 0.97 if y >= 1990 else 0.93


def floor_factor(floor: int | None, floors: int | None) -> float:
    if not floor:
        return 1.0
    if floor == 1:
        return 0.95
    if floors and floor == floors:
        return 0.97
    return 1.0


def listing_factor(x: dict) -> float:
    return condition_factor(x.get("condition")) * year_factor(x.get("year")) * floor_factor(x.get("floor"), x.get("floors"))


def fair_price(listing: dict, pool: list[dict]) -> dict:
    """Медиана приведённой цены м² по аналогам из выдачи × поправки объекта × поправка на торг.
    Новостройку сравниваем с новостройками, вторичку — со вторичкой (если аналогов хватает)."""
    market = [x for x in pool if bool(x.get("is_new")) == bool(listing.get("is_new"))]
    if len(market) >= 4:
        pool = market
    same = [x for x in pool if x["id"] != listing["id"] and x["rooms"] == listing["rooms"]
            and 0.7 <= x["area"] / listing["area"] <= 1.4]
    near = []
    if listing.get("lat"):
        dist = {x["id"]: km(listing, x) for x in same if x.get("lat")}
        near = sorted((x for x in same if x["id"] in dist and dist[x["id"]] <= 4), key=lambda x: dist[x["id"]])[:8]
    district = [x for x in same if listing.get("district") and x.get("district") == listing["district"]]
    if len(near) >= 4:
        comps, basis = near, f"{len(near)} похожих квартир в радиусе {max(1, round(dist[near[-1]['id']]))} км"
    elif len(district) >= 4:
        comps, basis = district, f"{len(district)} аналогов в том же районе"
    else:  # мало данных рядом — похожие по площади по городу
        comps = sorted(same or [x for x in pool if x["id"] != listing["id"]], key=lambda x: abs(x["area"] - listing["area"]))[:12]
        basis = f"{len(comps)} похожих по площади квартир по городу"
    if not comps:
        comps, basis = [listing], "аналогов нет — считаю по самому объекту"
    std = sorted(x["price"] / x["area"] / listing_factor(x) for x in comps)
    base_ppm2 = statistics.median(std)
    k = listing_factor(listing) * listing["area"] * BARGAIN_DISCOUNT
    fair = int(round(base_ppm2 * k, -4))
    # коридор — межквартильный размах аналогов (не уже ±2,5%)
    if len(std) >= 4:
        q1, _, q3 = statistics.quantiles(std, n=4)
    else:
        q1 = q3 = base_ppm2
    low = int(round(min(q1 * k, fair * 0.975), -4))
    high = int(round(max(q3 * k, fair * 1.025), -4))
    cheaper = [x for x in comps if x["price"] / x["area"] < listing["price"] / listing["area"]]
    return {
        "fair": fair,
        "low": low,
        "high": high,
        "target": int(round(min(fair * 0.985, listing["price"] * 0.975), -4)),
        "overprice_pct": round((listing["price"] - fair) / fair * 100, 1),
        "ppm2": int(base_ppm2 * listing_factor(listing)),
        "comps_count": len(comps),
        "cheaper_comps": len(cheaper),
        "basis": basis + ", поправка на торг −3%",
    }


def km(a: dict, b: dict) -> float:
    """Расстояние между объявлениями по координатам, км."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def bargaining_arguments(listing: dict, fp: dict) -> list[str]:
    args = []
    d = listing.get("days_on_market")
    if d and d >= 45:
        args.append(f"квартира в продаже {d} {plural(d, 'день', 'дня', 'дней')} — дольше обычного (~1,5 месяца)")
    if fp["overprice_pct"] > 0:
        args.append(f"по аналогам справедливая цена ~{fmt(fp['fair'])} — объявление выше на {fp['overprice_pct']}%")
    c = fp["cheaper_comps"]
    if c:
        args.append(f"{c} {plural(c, 'похожая квартира', 'похожие квартиры', 'похожих квартир')} в выдаче дешевле за м²")
    if listing.get("floor") == 1:
        args.append("первый этаж — такие квартиры обычно продаются дешевле")
    if listing.get("condition") in ("требует ремонта", "без отделки"):
        args.append(f"состояние: {listing['condition']} — покупателю нужны вложения в ремонт")
    if listing.get("year") and listing["year"] < 1990:
        args.append(f"дом {listing['year']} года")
    if listing.get("is_new"):
        args.append("уточню скидку застройщика при ипотеке или полной оплате — у застройщиков это частая практика")
    args.append("покупатель с одобренной ипотекой Сбера, деньги через сервис безопасных расчётов, выход на сделку за 2–3 недели")
    return args


def score_listing(x: dict, p: dict, fp: dict) -> float:
    value = max(-0.1, min(0.1, (fp["fair"] - x["price"]) / fp["fair"]))
    s = 30 * (0.5 + value / 0.2)
    if in_districts(x, p.get("districts")):
        s += 10  # район, который назвал клиент
    s += min(8, (x.get("days_on_market") or 0) / 15)
    s += {"дизайнерский": 10, "евроремонт": 10, "косметика": 6, "чистовая отделка": 5}.get(x.get("condition") or "", 3)
    m = x.get("metro") or {}
    minutes = m.get("minutes")
    if minutes is not None:
        walk = minutes if m.get("mode") == "пешком" else minutes * 2
        s += 8 if walk <= 10 else 5 if walk <= 20 else 2
    floor, floors = x.get("floor"), x.get("floors")
    if floor == 1 or (floors and floor == floors):
        s -= 6
    if x.get("year") and x["year"] < 1990:
        s -= 6
    kitchen = x.get("kitchen")
    if kitchen is not None and kitchen < 8:
        s -= 4
    if p.get("kitchen_min") and kitchen is not None and kitchen < p["kitchen_min"]:
        s -= 8
    if p.get("area_min") and x["area"] < p["area_min"]:
        s -= 8
    if p.get("metro_max_minutes") and minutes is not None and minutes > p["metro_max_minutes"]:
        s -= 6
    s += max(0, min(5, (x["area"] - 40) / 3))
    if x["price"] > p["budget_max"]:
        s -= 8
    if x.get("is_apartments"):
        s -= 10
    return round(s, 1)
