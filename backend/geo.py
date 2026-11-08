"""Что рядом с квартирой: школы, поликлиники, парки и т. п. по данным OpenStreetMap.

Клиент говорит «рядом школа», «хочу поликлинику рядом» — агент проверяет это по карте
и показывает только квартиры, где нужное есть в радиусе 1 км.

Живой запрос к Overpass API: один запрос на пачку квартир (до 25),
ответы кэшируются, пока работает сервер.

Проверить: .venv/bin/python -m backend.geo
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone

import httpx

from . import db
from .config import settings
from .sources import places
from .db import repo

log = logging.getLogger(__name__)

RADIUS = 1000  # м — «рядом»: 12–15 минут пешком
NEAR = 500     # м — «в шаге от дома»

# Серверы Overpass: главный, запасной — зеркало VK
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# без своего User-Agent главный сервер отвечает 406
HEADERS = {"User-Agent": "domclick-agent-prototype/0.2 (hackathon demo)"}

# ключ: (как назвать, запросы OSM)
POI = {
    "school": ("школа", ['nwr["amenity"="school"]']),
    "kindergarten": ("детский сад", ['nwr["amenity"="kindergarten"]']),
    "clinic": ("поликлиника", ['nwr["amenity"~"^(clinic|doctors)$"]', 'nwr["healthcare"~"^(clinic|doctor)$"]']),
    "hospital": ("больница", ['nwr["amenity"="hospital"]']),
    "park": ("парк", ['nwr["leisure"~"^(park|garden|nature_reserve)$"]["name"]', 'nwr["landuse"="forest"]["name"]']),
    "water": ("вода", ['nwr["natural"="water"]["name"]', 'way["waterway"~"^(river|canal)$"]["name"]']),
    "pharmacy": ("аптека", ['nwr["amenity"="pharmacy"]']),
    "shop": ("супермаркет", ['nwr["shop"="supermarket"]']),
    "mall": ("торговый центр", ['nwr["shop"="mall"]']),
    "fitness": ("фитнес", ['nwr["leisure"~"^(fitness_centre|sports_centre)$"]']),
}
# протяжённые объекты: храним контур (прореженный), расстояние — до берега или края парка
AREAS = {"park", "water"}
STEP_DEG = 0.0006  # ~60 м между сохранёнными точками контура

LIVE_CHUNK = 8     # небольшая пачка успевает в общий бюджет, частичные результаты сохраняются
POI_TTL_DAYS = 30  # что рядом с домом: пересчитываем раз в месяц
LIVE_TIMEOUT = 4   # каждый upstream должен оставить время зеркалу и обработке ответа

_live_cache: dict[tuple[float, float, str], dict | None] = {}
_cache_times: dict[tuple, float] = {}


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


# ------------------------------------------------------------------ запрос к карте

async def _overpass(query: str, timeout: float) -> list[dict]:
    """Запрос к Overpass: главный сервер, при ошибке — запасной."""
    from .runtime import record
    record("source_request", source="overpass")
    last: Exception | None = None
    for url in MIRRORS:
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as c:
                r = await c.post(url, data={"data": query}, headers=HEADERS)
            if r.status_code == 200:
                return r.json().get("elements", [])
            last = RuntimeError(f"{url}: HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            last = RuntimeError(f"{url}: {type(exc).__name__}")
        log.info("Overpass: %s", last)
    raise RuntimeError(f"карта недоступна ({last})")


def _row(e: dict, kind: str) -> list | None:
    """Элемент OSM -> [lat, lng, name] (точка) или [lat, lng, name, s, w, n, e, [[lat1, lng1, lat2, …], …]] (контуры)."""
    name = (e.get("tags") or {}).get("name") or POI[kind][0]
    if "lat" in e:
        return [round(e["lat"], 5), round(e["lon"], 5), name]
    if "center" in e:
        return [round(e["center"]["lat"], 5), round(e["center"]["lon"], 5), name]
    lines = [e["geometry"]] if e.get("geometry") else [m["geometry"] for m in e.get("members") or [] if m.get("geometry")]
    out: list[list[float]] = []
    for line in lines:
        nodes = [p for p in line if p]
        kept: list[float] = []
        for i, p in enumerate(nodes):
            last = i == len(nodes) - 1
            if not kept or last or abs(p["lat"] - kept[-2]) > STEP_DEG or abs(p["lon"] - kept[-1]) > STEP_DEG * 1.8:
                kept += [round(p["lat"], 5), round(p["lon"], 5)]
        if kept:
            out.append(kept)
    if not out:
        b = e.get("bounds")
        return [round((b["minlat"] + b["maxlat"]) / 2, 5), round((b["minlon"] + b["maxlon"]) / 2, 5), name] if b else None
    lats = [v for line in out for v in line[0::2]]
    lngs = [v for line in out for v in line[1::2]]
    mid = len(lats) // 2
    return [lats[mid], lngs[mid], name, min(lats), min(lngs), max(lats), max(lngs), out]


def _to_segment(lat: float, lng: float, a: tuple[float, float], b: tuple[float, float]) -> tuple[int, float, float]:
    """Расстояние от точки до отрезка ab (м) и ближайшая точка на нём."""
    kx = 111_320 * math.cos(math.radians(lat))
    ky = 110_540
    ax, ay = (a[1] - lng) * kx, (a[0] - lat) * ky
    bx, by = (b[1] - lng) * kx, (b[0] - lat) * ky
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return int(math.hypot(px, py)), lat + py / ky, lng + px / kx


def _query(kinds: list[str], areas_of_search: list[str], timeout: int) -> str:
    """Один запрос на все виды: у точечных (школа, аптека) — только центр, у парков и воды — контур."""
    def union(want_areas: bool) -> str:
        return "".join(f"{f}{where};" for k in kinds if (k in AREAS) == want_areas
                       for f in POI[k][1] for where in areas_of_search)
    return (f"[out:json][timeout:{timeout}];({union(False)})->.p;({union(True)})->.a;"
            ".p out center tags qt;.a out geom tags qt;")


def _rows(elements: list[dict], kinds: list[str]) -> dict[str, list]:
    """Ответ Overpass -> {вид: [строки _row]}; один объект может подойти под несколько видов."""
    out: dict[str, list] = {k: [] for k in kinds}
    seen: set[tuple] = set()
    for el in elements:
        tags = el.get("tags") or {}
        shaped = "geometry" in el or "members" in el  # пришёл с контуром (из .a)
        for kind in kinds:
            # точечные виды берём с центром (.p), парки и воду — с контуром (.a)
            if (kind in AREAS) != shaped and el.get("type") != "node":
                continue
            key = (el.get("type"), el.get("id"), kind)
            if key in seen or not _matches(tags, kind):
                continue
            seen.add(key)
            if row := _row(el, kind):
                out[kind].append(row)
    return out


# ------------------------------------------------------------------ поиск рядом с квартирой

def _nearest(lat: float, lng: float, rows: list[list]) -> dict | None:
    """Ближайший объект в радиусе: для контуров — ближайшая точка берега / края парка."""
    dlat = RADIUS / 111_000
    dlng = dlat / max(0.2, math.cos(math.radians(lat)))
    best = None
    for row in rows:
        if len(row) > 3:
            s, w, n, e, lines = row[3:8]
            if lat < s - dlat or lat > n + dlat or lng < w - dlng or lng > e + dlng:
                continue
            for pts in lines:
                if len(pts) == 2:  # одна точка
                    d, plat, plng = distance_m(lat, lng, pts[0], pts[1]), pts[0], pts[1]
                    if d <= RADIUS and (best is None or d < best["dist"]):
                        best = {"name": row[2], "dist": d, "lat": plat, "lng": plng}
                    continue
                for i in range(0, len(pts) - 2, 2):
                    a, b = (pts[i], pts[i + 1]), (pts[i + 2], pts[i + 3])
                    if (min(a[0], b[0]) - dlat > lat or max(a[0], b[0]) + dlat < lat
                            or min(a[1], b[1]) - dlng > lng or max(a[1], b[1]) + dlng < lng):
                        continue
                    d, plat, plng = _to_segment(lat, lng, a, b)
                    if d <= RADIUS and (best is None or d < best["dist"]):
                        best = {"name": row[2], "dist": d, "lat": round(plat, 5), "lng": round(plng, 5)}
        else:
            if abs(row[0] - lat) > dlat or abs(row[1] - lng) > dlng:
                continue
            d = distance_m(lat, lng, row[0], row[1])
            if d <= RADIUS and (best is None or d < best["dist"]):
                best = {"name": row[2], "dist": d, "lat": row[0], "lng": row[1]}
    return best


async def _live(listings: list[dict], kinds: list[str], on_progress=None) -> bool:
    """Спрашивает Overpass, что в 1 км от квартир (ответы кэшируются на время работы сервера)."""
    now = time.time()
    for key in list(_live_cache):
        if now - _cache_times.get(key, 0) >= POI_TTL_DAYS * 86400:
            _live_cache.pop(key, None)
            _cache_times.pop(key, None)
    if len(_live_cache) > 20_000:
        _live_cache.clear()
        _cache_times.clear()
    todo = [x for x in listings if any((round(x["lat"], 6), round(x["lng"], 6), k) not in _live_cache for k in kinds)]
    for i in range(0, len(todo), LIVE_CHUNK):
        chunk = todo[i:i + LIVE_CHUNK]
        around = [f"(around:{RADIUS},{x['lat']:.5f},{x['lng']:.5f})" for x in chunk]
        t0 = time.monotonic()
        try:
            elements = await _overpass(_query(kinds, around, LIVE_TIMEOUT), timeout=LIVE_TIMEOUT + 1)
        except RuntimeError as exc:
            log.warning("%s", exc)
            return False
        log.info("Карта: %d квартир, %d объектов за %.1f с", len(chunk), len(elements), time.monotonic() - t0)
        rows = _rows(elements, kinds)
        for x in chunk:
            for k in kinds:
                cache_key = (round(x["lat"], 6), round(x["lng"], 6), k)
                _live_cache[cache_key] = _nearest(x["lat"], x["lng"], rows[k])
                _cache_times[cache_key] = time.time()
            _apply_osm(x, kinds)
        if on_progress:on_progress()
    for x in listings:
        _apply_osm(x, kinds)
    return True


def _apply_osm(x, kinds):
    for k in kinds:
        cache_key = (round(x["lat"], 6), round(x["lng"], 6), k)
        if k not in (x.get("nearby") or {}) and cache_key in _live_cache:
            x.setdefault("nearby", {})[k] = _live_cache[cache_key]
            x.setdefault("nearby_evidence", {})[k] = {
                "source": "osm", "checked_at": datetime.fromtimestamp(_cache_times[cache_key], timezone.utc).isoformat(timespec="seconds"),
                "distance_kind": "edge" if k in AREAS else "straight_line", "radius": RADIUS}


def _matches(tags: dict, kind: str) -> bool:
    """Подходит ли объект OSM под один из запросов вида."""
    for flt in POI[kind][1]:
        for key, op, value in re.findall(r'\["(\w+)"(?:(=|~)"([^"]*)")?\]', flt):
            v = tags.get(key)
            if v is None or (op == "=" and v != value) or (op == "~" and not re.search(value, v)):
                break
        else:
            return True
    return False


async def nearby(listings: list[dict], kinds: list[str], budget_s: float | None = None, on_progress=None) -> bool:
    """None в nearby — проверено, не найдено; отсутствующий ключ — неизвестно.

    В БД сохраняем значение, источник, дату и координаты. Частичные результаты не теряются при таймауте.
    """
    kinds = [k for k in kinds if k in POI]
    if not kinds:
        return True
    budget = settings.places_budget_s if budget_s is None else max(0, budget_s)
    points = [x for x in listings if x.get("lat") is not None and x.get("lng") is not None]
    known = db.safe(repo.load_features, [x["id"] for x in points], prefix="poi:", max_age_days=POI_TTL_DAYS, default={}) or {}
    todo = []
    for x in points:
        for k in kinds:
            stored = known.get(x["id"], {})
            if f"poi:{k}" not in stored:
                continue
            record = stored[f"poi:{k}"]
            if isinstance(record, dict) and record.get("version") == 2:
                if record.get("coords") != [x['lat'], x['lng']]:
                    continue
                x.setdefault("nearby", {})[k] = record['value']
                x.setdefault("nearby_evidence", {})[k] = record['evidence']
            else:  # прежний формат кеша
                x.setdefault("nearby", {})[k] = record
                x.setdefault("nearby_evidence", {})[k] = {"source": "osm", "checked_at": None}
        if any(k not in (x.get("nearby") or {}) for k in kinds):
            todo.append(x)
    _local(todo, kinds)
    if on_progress:on_progress()

    async def fill():
        if not any(k not in (x.get('nearby') or {}) for x in todo for k in kinds):return
        if settings.twogis_api_key:
            try:
                operation=places.enrich(todo,kinds,RADIUS,on_progress=on_progress) if on_progress else places.enrich(todo,kinds,RADIUS)
                await asyncio.wait_for(operation, timeout=min(8, budget * .7))
            except asyncio.TimeoutError:
                pass
        missing = [x for x in todo if any(k not in (x.get("nearby") or {}) for k in kinds)]
        if missing:
            if on_progress:await _live(missing,kinds,on_progress=on_progress)
            else:await _live(missing,kinds)
    if todo and budget > 0:
        try:
            await asyncio.wait_for(fill(), timeout=budget)
        except asyncio.TimeoutError:
            log.info("бюджет карты исчерпан; сохраняю завершённые проверки")
    rows = []
    for x in todo:
        for k in kinds:
            if k in (x.get("nearby") or {}):
                record = {"version": 2, "value": x['nearby'][k], "evidence": x.get('nearby_evidence', {}).get(k, {}),
                          "coords": [x['lat'], x['lng']]}
                rows.append((x['id'], f"poi:{k}", record, "map"))
    db.safe(repo.save_features, rows)
    complete=all(all(k in (x.get("nearby") or {}) for k in kinds) for x in listings)
    from .telemetry import dependency
    dependency('map','available' if complete else 'degraded')
    return complete


def _local(listings: list[dict], kinds: list[str]) -> int:
    """Города-миллионники: что рядом — по своей выгрузке OpenStreetMap (backend/geodata), без внешних запросов.
    Возвращает, сколько проверок сделано локально."""
    from . import geodata
    from .runtime import record
    done = 0
    for x in listings:
        c = geodata.city(x.get("city"))
        if not c:
            continue
        for k in kinds:
            if k in (x.get("nearby") or {}):
                continue
            rows = geodata.poi_rows(x["city"], k)
            if not rows:  # пустой список — выгрузка неполная (сбой Overpass), а не «в городе нет поликлиник»
                continue
            found = _nearest(x["lat"], x["lng"], rows)
            if found is None:
                continue  # локальная выгрузка не доказывает отсутствие: остаётся живой запасной путь
            x.setdefault("nearby", {})[k] = found
            x.setdefault("nearby_evidence", {})[k] = {"source": "osm_local", "checked_at": c["built_at"],
                                                      "distance_kind": "edge" if k in AREAS else "straight_line", "radius": RADIUS}
            done += 1
    if done:
        record("source_request", source="poi_local", checks=done)
    return done


def has_all(x: dict, kinds: list[str]) -> bool:
    """Всё, что клиент хочет рядом, есть в радиусе 1 км."""
    near = x.get("nearby") or {}
    return all(near.get(k) for k in kinds)


def score(x: dict, kinds: list[str]) -> float:
    """Поправка к оценке: ближе 500 м — больше плюс."""
    near = x.get("nearby") or {}
    return sum(12 if near[k]["dist"] <= NEAR else 6 for k in kinds if near.get(k))


def summary(x: dict, kinds: list[str]) -> list[dict]:
    """Для карточки: [{kind, label, name, dist, lat, lng}]; dist None — в 1 км нет."""
    near = x.get("nearby") or {}
    out = []
    for k in kinds:
        if k in near:
            p = near[k] or {}
            out.append({"kind": k, "label": POI[k][0], "name": p.get("name"), "dist": p.get("dist"),
                        "lat": p.get("lat"), "lng": p.get("lng"),
                        **(x.get("nearby_evidence") or {}).get(k, {})})
    return out


async def _main() -> None:
    x = {"id": "test", "city": "Москва", "lat": 55.6744, "lng": 37.5560}
    kinds = list(POI)
    ok = await nearby([x], kinds)
    print("Черёмушки:", "проверено" if ok else "карта недоступна")
    for row in summary(x, kinds):
        print(" ", row["label"], "—", f"{row['name']}, {row['dist']} м" if row["dist"] is not None else "нет в 1 км")


if __name__ == "__main__":
    asyncio.run(_main())
