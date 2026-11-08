"""Геоданные городов-миллионников: районы, неофициальные районы, метро, «что рядом», крупные дороги.

Собираются скриптом scripts/build_geodata.py из OpenStreetMap (© участники OSM, ODbL) и лежат
в backend/geodata/cities/<slug>.json.gz (справочник) и <slug>.poi.json.gz (объекты и дороги).
Нет файла города — модуль отвечает «нет данных», и агент работает как раньше (модель, 2ГИС, Overpass).

Проверить, что собрано:  .venv/bin/python -m backend.geodata
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "cities"
OUT_DIR = Path(os.environ.get("GEODATA_DIR") or DEFAULT_DIR)  # тесты подставляют пустую папку

# Города-миллионники России (Росстат) и ближайшие к миллиону. relation — id границы в OSM, если поиск по имени
# находит не то; district_levels — уровни admin_level районов по порядку предпочтения; okrugs — округа Москвы.
CITIES: list[dict] = [
    {"name": "Москва", "district_levels": ["8"], "okrugs": True},
    {"name": "Санкт-Петербург", "district_levels": ["5"]},
    {"name": "Новосибирск"},
    {"name": "Екатеринбург"},
    {"name": "Казань"},
    {"name": "Красноярск"},
    {"name": "Нижний Новгород"},
    {"name": "Челябинск"},
    {"name": "Уфа"},
    {"name": "Самара"},
    {"name": "Ростов-на-Дону"},
    {"name": "Краснодар"},
    {"name": "Омск"},
    {"name": "Воронеж"},
    {"name": "Пермь"},
    {"name": "Волгоград"},
    {"name": "Саратов"},
    {"name": "Тюмень"},
]

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
                "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slug(name: str) -> str:
    out = "".join(_TR.get(ch, ch) for ch in name.lower())
    return "".join(ch if ch.isalnum() else "-" for ch in out).strip("-")


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError,ValueError,EOFError):
        logging.getLogger(__name__).warning('Локальная выгрузка %s недоступна; используется запасной источник',path.name)
        return None


@lru_cache(maxsize=32)
def city(name: str | None) -> dict | None:
    """Справочник города или None, если данных нет."""
    return _read(OUT_DIR / f"{slug(name)}.json.gz") if name else None


@lru_cache(maxsize=8)
def extra(name: str | None) -> dict | None:
    """Объекты «что рядом» и дороги города (большой файл — грузим по требованию)."""
    return _read(OUT_DIR / f"{slug(name)}.poi.json.gz") if name else None


def covered() -> list[str]:
    """Города, для которых собраны данные."""
    return [c["name"] for c in CITIES if (OUT_DIR / f"{slug(c['name'])}.json.gz").exists()]


def reset() -> None:
    city.cache_clear()
    extra.cache_clear()
    _index.cache_clear()


# ------------------------------------------------------------------ геометрия

def inside(lat: float, lng: float, rings: list[list[float]]) -> bool:
    hit = False
    for line in rings:
        for i in range(0, len(line) - 2, 2):
            y1, x1, y2, x2 = line[i], line[i + 1], line[i + 2], line[i + 3]
            if (y1 > lat) != (y2 > lat) and lng < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def district_of(city_name: str, lat: float | None, lng: float | None) -> dict | None:
    """Административный район, в котором точка (по границам OSM)."""
    c = city(city_name)
    if not c or lat is None or lng is None:
        return None
    for d in c["districts"]:
        b = d["bbox"]
        if b[0] <= lat <= b[2] and b[1] <= lng <= b[3] and inside(lat, lng, d["rings"]):
            return d
    return None


def _segment_m(lat: float, lng: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    kx, ky = 111_320 * math.cos(math.radians(lat)), 110_540
    ax, ay, bx, by = (a[1] - lng) * kx, (a[0] - lat) * ky, (b[1] - lng) * kx, (b[0] - lat) * ky
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest_line_m(city_name: str, kind: str, lat: float, lng: float, limit_m: float = 1000) -> float | None:
    """Расстояние до ближайшей крупной дороги (kind=major) или ж/д (rail), м; None — нет данных или дальше limit_m."""
    data = extra(city_name)
    if not data:
        return None
    dlat = limit_m / 111_000
    dlng = dlat / max(0.2, math.cos(math.radians(lat)))
    best = None
    for line in data["roads"].get(kind, []):
        for i in range(0, len(line) - 2, 2):
            a, b = (line[i], line[i + 1]), (line[i + 2], line[i + 3])
            if (min(a[0], b[0]) - dlat > lat or max(a[0], b[0]) + dlat < lat
                    or min(a[1], b[1]) - dlng > lng or max(a[1], b[1]) + dlng < lng):
                continue
            d = _segment_m(lat, lng, a, b)
            if d <= limit_m and (best is None or d < best):
                best = d
    return None if best is None else round(best)


def poi_rows(city_name: str | None, kind: str) -> list | None:
    """Объекты вида kind по городу в формате backend.geo или None, если данных нет."""
    data = extra(city_name)
    return None if not data else data["poi"].get(kind)


# ------------------------------------------------------------------ названия

def norm(text: str) -> str:
    return " ".join((text or "").casefold().replace("ё", "е").replace("-", " ").split())


@lru_cache(maxsize=1)
def _index() -> dict[str, list[dict]]:
    """Все названия всех собранных городов -> сущности: город, район, неофициальный район, метро."""
    from .names import CITY_ALIASES, PLACE_ALIASES
    idx: dict[str, list[dict]] = {}

    def add(name: str, entity: dict) -> None:
        key = norm(name)
        if key and entity not in idx.setdefault(key, []):
            idx[key].append(entity)
    from ..sources import SUPPORTED_CITIES
    for name in [*SUPPORTED_CITIES, *(c["name"] for c in CITIES)]:  # все города Циана, районы — для миллионников
        add(name, {"type": "city", "city": name, "name": name})
    for alias, name in CITY_ALIASES.items():
        add(alias, {"type": "city", "city": name, "name": name})
    for name in covered():
        c = city(name)
        for d in c["districts"]:
            add(d["name"], {"type": "district", "city": name, "name": d["name"], "okrug": d.get("okrug")})
        for p in c["places"]:
            add(p["name"], {"type": "place", "city": name, "name": p["name"], "district": p.get("district"),
                            "lat": p["lat"], "lng": p["lng"]})
        for m in c["metro"]:
            add(m["name"], {"type": "metro", "city": name, "name": m["name"], "district": m.get("district"),
                            "lat": m["lat"], "lng": m["lng"]})
    for alias, target in PLACE_ALIASES.items():
        if target["city"] not in covered():
            continue
        if target.get("districts"):  # обиходное имя нескольких районов: «Бутово» = Северное и Южное Бутово
            add(alias, {"type": "area", "city": target["city"], "name": alias.capitalize(), "districts": target["districts"]})
            continue
        for e in list(idx.get(norm(target["name"]), [])):
            if e["city"] == target["city"]:
                add(alias, e)
    return idx


def lookup(name: str) -> list[dict]:
    """Сущности по названию в именительном падеже (синонимы учтены)."""
    return list(_index().get(norm(name), []))


def all_names() -> list[str]:
    return list(_index())


def report() -> None:
    """Что собрано по городам (python -m backend.geodata)."""
    done = covered()
    print(f"Собрано городов: {len(done)} из {len(CITIES)}")
    for c in CITIES:
        d = city(c["name"])
        if not d:
            print(f"  {c['name']}: нет данных — .venv/bin/python -m scripts.build_geodata --cities {c['name']}")
            continue
        x = extra(c["name"]) or {"poi": {}, "roads": {}}
        print(f"  {c['name']}: районов {len(d['districts'])}, неофициальных {len(d['places'])}, метро {len(d['metro'])}, "
              f"объектов рядом {sum(len(v) for v in x['poi'].values())}, дорог {len(x['roads'].get('major', []))}, "
              f"собрано {d['built_at'][:10]}")
