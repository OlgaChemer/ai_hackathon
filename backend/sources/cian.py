"""Циан: поиск объявлений через обычную страницу выдачи (без телефонов).

Официального API для поиска чужих объявлений у Циана нет, поэтому берём
данные, которые сайт встраивает в страницу поиска. Правила прототипа:
- запрос только по просьбе клиента, 1–2 страницы, не чаще раза в 3 секунды;
- телефоны и данные продавца не сохраняем, фото — ссылкой на оригинал;
- капча или блокировка -> не обходим, а берём сохранённый срез или демо-базу.

Прогрев среза перед демо:
    .venv/bin/python -m backend.sources.cian --city Казань --rooms 2 --budget 9000000
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import httpx

from .base import SourceUnavailable

log = logging.getLogger(__name__)

SOURCE = "cian"
SOURCE_NAME = "Циан"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "cian"
CACHE_TTL = 30 * 60
SNAPSHOT_VERSION = 2  # 2 — без долей; срезы другой версии не используем
MIN_INTERVAL = 3.0

# город -> (код региона, поддомен, город в предложном падеже для проверки заголовка)
REGIONS = {
    "Москва": (1, "www", "Москв"),
    "Санкт-Петербург": (2, "spb", "Санкт-Петербург"),
    "Казань": (4777, "kazan", "Казан"),
    "Новосибирск": (4897, "novosibirsk", "Новосибирск"),
    "Екатеринбург": (4743, "ekb", "Екатеринбург"),
    "Нижний Новгород": (4885, "nn", "Нижн"),
    "Краснодар": (4820, "krasnodar", "Краснодар"),
}

# Округа Москвы: номер в фильтре Циана (district[0]=...) — проверено по данным выдачи
MOSCOW_OKRUGS = {"СЗАО": 1, "ЦАО": 4, "САО": 5, "СВАО": 6, "ВАО": 7, "ЮВАО": 8, "ЮАО": 9, "ЮЗАО": 10, "ЗАО": 11}

REPAIR = {"cosmetic": "косметика", "euro": "евроремонт", "design": "дизайнерский", "no": "требует ремонта"}
DECORATION = {"rough": "без отделки", "without": "без отделки", "preFine": "предчистовая", "fine": "чистовая отделка"}
MATERIAL = {
    "brick": "кирпич", "monolith": "монолит", "panel": "панель", "monolithBrick": "монолит-кирпич",
    "block": "блочный", "wood": "дерево", "stalin": "сталинский", "old": "старый фонд",
}

# состояние из описания, если поле «ремонт» не заполнено (порядок важен)
CONDITION_WORDS = [
    (r"дизайн\w*\s+ремонт", "дизайнерский"),
    (r"евро\s*-?\s*ремонт", "евроремонт"),
    (r"требует\s+(?:капитальн\w*\s+|косметическ\w*\s+)?ремонт|под\s+ремонт|без\s+ремонта", "требует ремонта"),
    (r"черновой\s+отделк|без\s+отделки", "без отделки"),
    (r"чистов\w*\s+отделк", "чистовая отделка"),
    (r"(?:свеж\w*|косметическ\w*|хорош\w*)\s+ремонт|(?:хорош|отличн)\w*\s+состояни", "косметика"),
]


def condition_from_text(text: str | None) -> str | None:
    t = (text or "").lower()
    return next((name for pattern, name in CONDITION_WORDS if re.search(pattern, t)), None)


_memory: dict[tuple, tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()
_last_request = 0.0


def supported(city: str | None) -> bool:
    return city in REGIONS


def build_url(city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None) -> str:
    """Свежие объявления вторички, только целые квартиры (flat_share=2 — без долей);
    сортировка по дате — без перекоса в продвигаемые премиум-объекты."""
    region, sub, _ = REGIONS[city]
    room_param = "room9=1" if rooms == 0 else f"room{min(rooms, 5)}=1"
    url = (f"https://{sub}.cian.ru/cat.php?deal_type=sale&engine_version=2&offer_type=flat"
           f"&region={region}&{room_param}&object_type%5B0%5D=1&flat_share=2&sort=creation_date_desc&p={page}")
    if budget:
        url += f"&maxprice={int(budget * 1.05)}"
    if city == "Москва" and okrug in MOSCOW_OKRUGS:
        url += f"&district%5B0%5D={MOSCOW_OKRUGS[okrug]}"
    return url


# ------------------------------------------------------------------ разбор страницы


def _json_array_at(s: str, start: int) -> str | None:
    """Вырезает JSON-массив, начинающийся с s[start] == '[' (учитывает строки)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def extract_offers(page: str) -> list[dict]:
    for m in re.finditer(r'"offers":\[', page):
        raw = _json_array_at(page, m.end() - 1)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data and isinstance(data[0], dict) and "totalArea" in data[0]:
            return data
    return []


def _float(v) -> float | None:
    try:
        return round(float(v), 1) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _address(geo: dict, city: str) -> str:
    parts = [a.get("name") or a.get("fullName") for a in geo.get("address", []) if a.get("geoType") in ("street", "house")]
    if parts:
        return ", ".join(p for p in parts if p)
    text = geo.get("userInput") or ""
    return text.split(f"{city},")[-1].strip() or text


def normalize(o: dict, city: str, today: date | None = None) -> dict | None:
    today = today or date.today()
    terms = o.get("bargainTerms") or {}
    price = terms.get("priceRur") or terms.get("price")
    area = _float(o.get("totalArea"))
    if not price or not area or terms.get("saleType") == "fz214":
        return None
    if o.get("shareAmount") or o.get("category") == "flatShareSale":  # продажа доли, а не квартиры
        return None
    if area < 18 or area < 9 * max(1, o.get("roomsCount") or 1):  # площадь комнаты вместо квартиры и т.п.
        return None
    geo = o.get("geo") or {}
    building = o.get("building") or {}
    districts = geo.get("districts") or []
    raion = next((d.get("name") for d in districts if d.get("type") == "raion"), None)
    district = raion or (districts[0].get("name") if districts else None)
    okrug = next((d.get("name") for d in districts if d.get("type") == "okrug"), None)
    metro = None
    if geo.get("undergrounds"):
        u = geo["undergrounds"][0]
        metro = {"name": u.get("name"), "minutes": u.get("time"),
                 "mode": "пешком" if u.get("transportType") == "walk" else "транспортом"}
    try:
        created = datetime.fromisoformat((o.get("creationDate") or "")[:19]).date()
        days = max(0, (today - created).days)
    except ValueError:
        days = None
    photos = o.get("photos") or []
    photo = (photos[0].get("thumbnail2Url") or photos[0].get("fullUrl")) if photos else None
    condition = REPAIR.get(o.get("repairType")) or DECORATION.get(o.get("decoration"))
    if not condition:  # поле «ремонт» не заполнено — ищем в описании
        condition = condition_from_text(o.get("description"))
    coords = geo.get("coordinates") or {}
    return {
        "id": f"cian-{o.get('cianId') or o.get('id')}",
        "source": SOURCE,
        "source_name": SOURCE_NAME,
        "url": (o.get("fullUrl") or "").split("?")[0] or None,
        "city": city,
        "district": district,
        "okrug": okrug,
        "address": _address(geo, city),
        "rooms": o.get("roomsCount") or 0,
        "area": area,
        "kitchen": _float(o.get("kitchenArea")),
        "floor": o.get("floorNumber"),
        "floors": building.get("floorsCount"),
        "year": building.get("buildYear"),
        "condition": condition,
        "lat": coords.get("lat"),
        "lng": coords.get("lng"),
        "material": MATERIAL.get(building.get("materialType")),
        "price": int(price),
        "days_on_market": days,
        "metro": metro,
        "photo": photo,
        "description": (o.get("description") or "")[:1500],
        "rosreestr_checked": bool(o.get("isRosreestrChecked")) if o.get("isRosreestrChecked") is not None else None,
        "is_apartments": bool(o.get("isApartments")),
        "is_auction": bool(o.get("isAuction")),
        "seller_role": "собственник" if o.get("isByHomeowner") else "агент продавца",
    }


# ------------------------------------------------------------------ загрузка


async def _get_httpx(url: str) -> tuple[int, str, str]:
    # local_address=0.0.0.0 — принудительно IPv4: в WSL httpx иначе виснет на подключении
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
                                 headers={"Accept-Language": "ru-RU,ru;q=0.9"}) as http:
        r = await http.get(url)
    return r.status_code, r.headers.get("location", ""), r.text


async def _get_browser_like(url: str) -> tuple[int, str, str]:
    """Запасной путь: TLS-отпечаток Chrome (curl_cffi). Без него Циан чаще отвечает капчей."""
    from curl_cffi.requests import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=20) as http:
        r = await http.get(url, allow_redirects=False, headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    return r.status_code, r.headers.get("location", ""), r.text


def _blocked(status: int, location: str) -> str | None:
    if status in (301, 302) and "captcha" in location:
        return "капча"
    if status != 200:
        return f"ответ {status}"
    return None


async def _fetch_page(city: str, rooms: int, budget: int | None, page: int, okrug: str | None = None) -> tuple[list[dict], str]:
    global _last_request
    url = build_url(city, rooms, budget, page, okrug)
    async with _lock:
        attempts = [("httpx", _get_httpx)]
        try:
            import curl_cffi  # noqa: F401  — запасной путь, если пакет установлен
            attempts.append(("curl_cffi", _get_browser_like))
        except ImportError:
            pass
        problem = "нет ответа"
        for name, getter in attempts:
            wait = MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request = time.monotonic()
            try:
                status, location, text = await getter(url)
            except Exception as exc:  # сеть, TLS — пробуем следующий способ
                problem = f"{name}: {type(exc).__name__}"
                continue
            problem = _blocked(status, location)
            if problem is None:
                break
            log.info("Циан через %s: %s — пробую дальше", name, problem)
        else:
            raise SourceUnavailable(f"Циан недоступен ({problem})")
    title = re.search(r"<title>([^<]*)", text)
    if not title or REGIONS[city][2].lower() not in title.group(1).lower():
        raise SourceUnavailable("страница не про нужный город")
    raw = extract_offers(text)
    if not raw and "offers" not in text:  # страница не та (вёрстка поменялась) — а не «ничего не нашлось»
        raise SourceUnavailable("не удалось разобрать страницу")
    offers = [x for x in (normalize(o, city) for o in raw) if x]
    return offers, name


def _snapshot_path(city: str, rooms: int) -> Path:
    return CACHE_DIR / f"{REGIONS[city][0]}_{rooms}.json"


def _okrug_ok(x: dict, okrug: str | None) -> bool:
    return not okrug or x.get("okrug") == okrug


def _save_snapshot(city: str, rooms: int, listings: list[dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _snapshot_path(city, rooms)
        old = load_snapshot(city, rooms)[0]
        merged = {x["id"]: x for x in old} | {x["id"]: x for x in listings}
        path.write_text(json.dumps({"version": SNAPSHOT_VERSION, "fetched_at": datetime.now().isoformat(timespec="minutes"),
                                    "listings": list(merged.values())}, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось сохранить срез Циана: %s", exc)


def load_snapshot(city: str, rooms: int) -> tuple[list[dict], str | None]:
    path = _snapshot_path(city, rooms)
    if not path.exists():
        return [], None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SNAPSHOT_VERSION:
        return [], None
    return data["listings"], data.get("fetched_at")


async def search(city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None) -> tuple[list[dict], dict]:
    """Возвращает (объявления, отчёт). Отчёт: status live | cached | snapshot | unavailable."""
    if not supported(city):
        raise SourceUnavailable(f"для города «{city}» площадка не подключена")
    key = (city, rooms, budget, page, okrug)
    hit = _memory.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], {"status": "cached", "note": "из кеша сессии"}
    try:
        listings, via = await _fetch_page(city, rooms, budget, page, okrug)
        _memory[key] = (time.time(), listings)
        _save_snapshot(city, rooms, listings)
        note = f"страница {page}" + (" (запасной путь curl_cffi после капчи)" if via == "curl_cffi" else "")
        return listings, {"status": "live", "note": note, "via": via}
    except SourceUnavailable as exc:
        log.warning("Циан недоступен: %s", exc)
        if page > 1:
            raise
        saved, fetched_at = load_snapshot(city, rooms)
        saved = [x for x in saved if (not budget or x["price"] <= budget * 1.05) and _okrug_ok(x, okrug)]
        if saved:
            return saved, {"status": "snapshot", "note": f"{exc}; сохранённый срез от {fetched_at}"}
        raise


async def market(city: str, rooms: int, okrug: str | None = None) -> dict | None:
    """Сколько стоят такие квартиры: квартили цены по свежим объявлениям (без фильтра по бюджету)."""
    import statistics

    try:
        items, rep = await search(city, rooms, None, 1, okrug)
    except SourceUnavailable:
        return None
    items = drop_outliers(items)
    if len(items) < 6:
        return None
    prices = sorted(x["price"] for x in items)
    q1, med, q3 = statistics.quantiles(prices, n=4)
    ppm2 = statistics.median(x["price"] / x["area"] for x in items)
    return {"count": len(items), "p25": int(q1), "median": int(med), "p75": int(q3), "ppm2": int(ppm2),
            "min": prices[0], "max": prices[-1], "status": rep["status"]}


def drop_outliers(items: list[dict]) -> list[dict]:
    """Доли, комнаты и ошибки в цене: цена м² в 3 раза ниже медианы по выборке."""
    import statistics

    if len(items) < 5:
        return items
    med = statistics.median(x["price"] / x["area"] for x in items)
    return [x for x in items if x["price"] / x["area"] >= med * 0.33]


if __name__ == "__main__":  # прогрев среза
    import argparse

    ap = argparse.ArgumentParser(description="Сохранить срез объявлений Циана для демо")
    ap.add_argument("--city", default="Казань")
    ap.add_argument("--rooms", type=int, default=2)
    ap.add_argument("--budget", type=int, default=9_000_000)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--okrug", default=None, help="для Москвы: ЮЗАО, ЦАО, …")
    args = ap.parse_args()

    async def main():
        total = 0
        for p in range(1, args.pages + 1):
            items, report = await search(args.city, args.rooms, args.budget, p, args.okrug)
            total += len(items)
            print(f"страница {p}: {len(items)} объявлений ({report['status']})")
        print(f"готово: {total}, срез — {_snapshot_path(args.city, args.rooms)}")

    asyncio.run(main())
