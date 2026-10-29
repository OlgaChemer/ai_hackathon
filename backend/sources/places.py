"""Что рядом с домом: садики, школы, парки и метро по координатам объявления.

Источник — OpenStreetMap через Nominatim: бесплатно, без ключа, работает из этой
сети. Overpass (bulk-выгрузка POI) и 2GIS отсюда недоступны, Яндекс.Геокодер
на присланном ключе отвечает 403 «Invalid api key» — ключ оставлен в .env
как YANDEX_MAPS_API_KEY на будущее (JS-карта или платный Places API).

Правила вежливости к Nominatim: не чаще одного запроса в секунду, свой
User-Agent, результаты кешируются на диск по сетке ~110 м и живут месяц.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import re

import httpx

from ..config import ROOT  # noqa: F401 — импорт загружает .env (ключ 2GIS)


log = logging.getLogger(__name__)

SOURCE = "osm"
URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "domclick-agent-hackathon/0.1 (prototype; olgakamskaya@gmail.com)"
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "places"
TWOGIS_KEY = os.getenv("TWOGIS_API_KEY", "").strip()
TWOGIS_URL = "https://catalog.api.2gis.com/3.0/items"
TTL = timedelta(days=30)
MIN_INTERVAL = 1.1          # секунд между запросами к Nominatim
DEFAULT_RADIUS = 1500       # метров: дальше не считаем «рядом»

# что ищем: ключ критерия -> поисковая фраза, радиус и рубрика 2GIS.
# Рубрики важны: по тексту «детский сад» 2GIS отдаёт и частные центры развития,
# а рубрика 237 — обычные (муниципальные) детские сады.
KINDS = {
    "садик": {"q": "детский сад", "radius": 1200, "rubric": "237"},
    "школа": {"q": "школа", "radius": 1200, "rubric": "245"},
    "парк": {"q": "парк", "radius": 1500, "rubric": "168"},
    "метро": {"q": "метро", "radius": 2000, "rubric": "113944"},
}

# Страховка для запасного источника: частные садики и центры развития не берём,
# если клиент просто сказал «садик».
PRIVATE_RE = re.compile(r"частн|центр развития|монтессори|полилингв|международн|языков", re.I)
MUNICIPAL_RE = {
    "садик": re.compile(r"детский сад|мбдоу|мадоу|д/с|сад\s*№", re.I),
    "школа": re.compile(r"школ|лицей|гимнази", re.I),
}


def is_municipal(kind: str, name: str) -> bool:
    pattern = MUNICIPAL_RE.get(kind)
    if pattern is None:
        return True
    return bool(pattern.search(name or "")) and not PRIVATE_RE.search(name or "")

_lock = asyncio.Lock()
_last = 0.0
_2gis = "unknown"  # unknown | up | down: 2GIS блокирует не-российские адреса


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Расстояние по прямой в метрах."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_minutes(distance_m: float) -> int:
    """Пешком ~4,8 км/ч: 80 м в минуту, минимум минута."""
    return max(1, round(distance_m / 80))


def _cache_path(lat: float, lng: float, kind: str) -> Path:
    return CACHE / f"{lat:.3f}_{lng:.3f}_{kind}.json"


def _read_cache(lat: float, lng: float, kind: str) -> list[dict] | None:
    path = _cache_path(lat, lng, kind)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if datetime.now() - datetime.fromisoformat(data["fetched_at"]) > TTL:
        return None
    return data.get("pois")


def _write_cache(lat: float, lng: float, kind: str, pois: list[dict]) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        _cache_path(lat, lng, kind).write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "pois": pois}, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось сохранить кеш мест: %s", exc)


def twogis_params(lat: float, lng: float, kind: str) -> dict:
    """Параметры поиска в 2GIS: рубрика важнее текста — она отсекает частников."""
    spec = KINDS[kind]
    return {"rubric_id": spec["rubric"], "q": spec["q"], "point": f"{lng},{lat}",
            "radius": spec["radius"], "key": TWOGIS_KEY, "page_size": 10, "sort": "distance",
            "fields": "items.point,items.name,items.rubrics"}


async def _fetch_2gis(lat: float, lng: float, kind: str) -> list[dict]:
    """Каталог 2GIS: точнее по России, но работает только с российских адресов."""
    params = twogis_params(lat, lng, kind)
    async with httpx.AsyncClient(timeout=6) as http:
        r = await http.get(TWOGIS_URL, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"2gis {r.status_code}")
    items = ((r.json().get("result") or {}).get("items") or [])
    pois = []
    for it in items:
        point = it.get("point") or {}
        if point.get("lat") is None or point.get("lon") is None:
            continue
        pois.append({"name": it.get("name") or "", "lat": float(point["lat"]), "lng": float(point["lon"])})
    return pois


async def _fetch_osm(lat: float, lng: float, kind: str) -> list[dict]:
    global _last
    spec = KINDS[kind]
    query, radius = spec["q"], spec["radius"]
    d = radius / 111_000  # ~градусы широты
    dl = radius / (111_000 * max(0.2, math.cos(math.radians(lat))))
    params = {
        "q": query, "format": "json", "limit": 10, "bounded": 1, "addressdetails": 0,
        "viewbox": f"{lng - dl},{lat + d},{lng + dl},{lat - d}",
    }
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
            r = await http.get(URL, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"nominatim {r.status_code}")
    pois = []
    for item in r.json():
        try:
            name = (item.get("display_name") or "").split(",")[0].strip()
            if not is_municipal(kind, name):
                continue
            pois.append({"name": name, "lat": float(item["lat"]), "lng": float(item["lon"])})
        except (KeyError, TypeError, ValueError):
            continue
    return pois


async def _pois(lat: float, lng: float, kind: str) -> list[dict]:
    global _2gis
    cached = _read_cache(lat, lng, kind)
    if cached is not None:
        return cached
    pois = None
    if TWOGIS_KEY and _2gis != "down":
        try:
            pois = await _fetch_2gis(lat, lng, kind)
            _2gis = "up"
        except Exception as exc:  # гео-блок, сеть, лимит — работаем дальше на OSM
            _2gis = "down"
            log.warning("2GIS недоступен (%s: %s) — считаю локацию по OpenStreetMap", type(exc).__name__, exc or "нет ответа")
    if pois is None:
        pois = await _fetch_osm(lat, lng, kind)
    _write_cache(lat, lng, kind, pois)
    return pois


async def nearby(lat: float | None, lng: float | None, kinds: list[str], budget_s: float = 10.0) -> dict:
    """Ближайший объект каждого вида: {"садик": {"name", "distance_m", "walk_min"}, ...}.

    Укладывается в бюджет времени: что не успели — просто отсутствует в ответе,
    и критерий по нему останется «нет данных», а не превратится в промах.
    """
    if lat is None or lng is None:
        return {}
    kinds = [k for k in kinds if k in KINDS]
    if not kinds:
        return {}

    async def one(kind: str) -> tuple[str, dict | None]:
        try:
            pois = await _pois(lat, lng, kind)
        except Exception as exc:  # сеть, лимит, 5xx — не ломаем показ
            log.info("рядом(%s): %s", kind, exc)
            return kind, None
        best = None
        for p in pois:
            dist = haversine(lat, lng, p["lat"], p["lng"])
            if dist <= KINDS[kind]["radius"] and (best is None or dist < best["distance_m"]):
                best = {"name": p["name"], "distance_m": round(dist), "walk_min": walk_minutes(dist),
                        "lat": p["lat"], "lng": p["lng"]}
        return kind, best

    results = await asyncio.gather(*(one(k) for k in kinds))
    return {k: v for k, v in results if v}


def status() -> dict:
    """Что используется для карты: 2GIS, если он отвечает, иначе OpenStreetMap."""
    provider = {"up": "2gis", "down": "osm"}.get(_2gis, "auto" if TWOGIS_KEY else "osm")
    return {
        "provider": provider,
        "2gis": _2gis,
        "2gis_key": bool(TWOGIS_KEY),
        "cache": str(CACHE),
    }


def map_link(lat: float | None, lng: float | None) -> str | None:
    """Ссылка на карту по координатам — без ключа и без встраивания виджета."""
    if lat is None or lng is None:
        return None
    return f"https://yandex.ru/maps/?ll={lng:.6f},{lat:.6f}&z=16&pt={lng:.6f},{lat:.6f}"
