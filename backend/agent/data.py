"""Синтетические данные для демо: объявления, проверки ЕГРН, закрытые сделки.

В проде это база объявлений Домклик, сделки Сбера и отчёты об оценке залогов.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TODAY = date(2026, 10, 20)

# Базовая цена 1 м² «стандартной» квартиры по районам Казани (допущение для демо).
DISTRICT_PPM2 = {
    "Вахитовский": 215_000,
    "Ново-Савиновский": 172_000,
    "Советский": 165_000,
    "Приволжский": 160_000,
    "Московский": 148_000,
    "Авиастроительный": 128_000,
    "Кировский": 136_000,
}
MONTHLY_TREND = 0.004  # рост цен ~0,4% в месяц


def condition_factor(condition: str) -> float:
    return {"евроремонт": 1.06, "косметика": 1.0, "требует ремонта": 0.92}.get(condition, 1.0)


def year_factor(year: int) -> float:
    if year >= 2015:
        return 1.03
    if year >= 2000:
        return 1.0
    if year >= 1990:
        return 0.97
    return 0.93


def floor_factor(floor: int, floors: int) -> float:
    if floor == 1:
        return 0.95
    if floor == floors:
        return 0.97
    return 1.0


@lru_cache
def listings() -> list[dict]:
    return json.loads((DATA_DIR / "listings.json").read_text(encoding="utf-8"))


@lru_cache
def egrn() -> dict:
    data = json.loads((DATA_DIR / "egrn.json").read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


def listing_by_id(listing_id: str) -> dict | None:
    return next((x for x in listings() if x["id"] == listing_id), None)


@lru_cache
def deals() -> list[dict]:
    """Закрытые сделки за 9 месяцев — генерируются детерминированно (seed)."""
    rnd = random.Random(2026)
    out = []
    conditions = ["евроремонт", "косметика", "косметика", "требует ремонта"]
    for district, base in DISTRICT_PPM2.items():
        for i in range(48):
            rooms = rnd.choice([1, 2, 2, 2, 3])
            area = round({1: rnd.uniform(33, 42), 2: rnd.uniform(44, 60), 3: rnd.uniform(62, 80)}[rooms], 1)
            year = rnd.choice([1965, 1978, 1988, 1996, 2004, 2008, 2012, 2016, 2019])
            floors = rnd.choice([5, 9, 10, 12, 16])
            floor = rnd.randint(1, floors)
            cond = rnd.choice(conditions)
            months_ago = rnd.uniform(0, 9)
            d = TODAY - timedelta(days=int(months_ago * 30))
            ppm2 = (
                base
                * condition_factor(cond)
                * year_factor(year)
                * floor_factor(floor, floors)
                * (1 - MONTHLY_TREND * months_ago)
                * rnd.uniform(0.955, 1.045)
            )
            out.append(
                {
                    "id": f"deal-{district[:3]}-{i}",
                    "district": district,
                    "rooms": rooms,
                    "area": area,
                    "year": year,
                    "floor": floor,
                    "floors": floors,
                    "condition": cond,
                    "date": d.isoformat(),
                    "price": int(round(ppm2 * area, -3)),
                }
            )
    return out
