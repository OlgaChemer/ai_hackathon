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
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .base import CIAN_REGIONS, SourceUnavailable, city_stem

log = logging.getLogger(__name__)

SOURCE = "cian"
SOURCE_NAME = "Циан"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "cian"
CACHE_TTL = 30 * 60
SNAPSHOT_VERSION = 2  # 2 — без долей; срезы другой версии не используем
MIN_INTERVAL = 3.0
FETCH_TIMEOUT = 35.0
FAILURE_COOLDOWN = 60.0
REDIRECTS = (301, 302, 303, 307, 308)

# город -> (код региона, поддомен, основа названия для проверки заголовка страницы).
# Поддомены семи городов проверены; остальные Циан называет сам редиректом — запоминаем (SUBS_FILE).
SUBDOMAINS = {"Москва": "www", "Санкт-Петербург": "spb", "Казань": "kazan", "Новосибирск": "novosibirsk",
              "Екатеринбург": "ekb", "Нижний Новгород": "nn", "Краснодар": "krasnodar"}
TITLE_STEMS = {"Великий Новгород": "велик", "Московская область": "московск", "Москва": "москв", "Нижний Новгород": "нижн"}
REGIONS = {city: (region, SUBDOMAINS.get(city, "www"), TITLE_STEMS.get(city) or city_stem(city))
           for city, region in CIAN_REGIONS.items()}
FEATURED_CITIES = list(SUBDOMAINS)  # проверены на живом Циане — их предлагаем кнопками

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
_failures: dict[tuple[str, bool], tuple[float, str]] = {}


def supported(city: str | None) -> bool:
    return city in REGIONS


SUBS_FILE = CACHE_DIR / "subdomains.json"
_subs: dict[str, str] | None = None


def _learned_subdomains() -> dict[str, str]:
    """Поддомены, которые Циан назвал редиректом (www.cian.ru?region=4780 -> kaluga.cian.ru)."""
    global _subs
    if _subs is None:
        try:
            _subs = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _subs = {}
    return _subs


def _learn_subdomain(city: str, sub: str) -> None:
    subs = _learned_subdomains()
    if subs.get(city) == sub:
        return
    subs[city] = sub
    log.info("Циан: город %s живёт на %s.cian.ru — запомнил", city, sub)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось запомнить поддомен Циана: %s", exc)


def redirect_target(url: str, location: str) -> str | None:
    """Куда Циан перенаправил запрос — если это не капча и не чужой сайт. Иначе None."""
    if not location or "captcha" in location.lower():
        return None
    target = urljoin(url, location)
    host = urlparse(target).hostname or ""
    return target if urlparse(target).scheme == "https" and (host == "cian.ru" or host.endswith(".cian.ru")) else None


def build_url(city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None, new: bool = False) -> str:
    """Свежие объявления, только целые квартиры (flat_share=2 — без долей); object_type: 1 — вторичка, 2 — новостройки;
    сортировка по дате — без перекоса в продвигаемые премиум-объекты."""
    region, sub, _ = REGIONS[city]
    sub = _learned_subdomains().get(city, sub)
    room_param = '' if rooms is None else "room9=1" if rooms == 0 else f"room{min(rooms, 5)}=1"
    url = (f"https://{sub}.cian.ru/cat.php?deal_type=sale&engine_version=2&offer_type=flat"
           f"&region={region}&{room_param}&object_type%5B0%5D={2 if new else 1}&flat_share=2&sort=creation_date_desc&p={page}")
    if budget:
        url += f"&maxprice={int(budget * 1.05)}"
    if city == "Москва" and okrug in MOSCOW_OKRUGS:
        url += f"&district%5B0%5D={MOSCOW_OKRUGS[okrug]}"
    return url


# ------------------------------------------------------------------ разбор страницы


def _json_array_at(s: str, start: int) -> str | None:
    """Вырезает JSON-массив или объект, начинающийся с s[start] ('[' или '{'), с учётом строк."""
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


def extract_offer(page: str) -> dict | None:
    """Объявление со страницы объявления: offerData.offer — те же поля, что в выдаче поиска."""
    m = re.search(r'"offerData":\{"offer":\{', page)
    raw = _json_array_at(page, m.end() - 1) if m else None
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "totalArea" in data else None


def extract_offers(page: str) -> list[dict]:
    for m in re.finditer(r'"offers"\s*:\s*\[', page):
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


def _count(o: dict, *keys: str) -> int | None:
    """Сумма счётчиков из объявления; None — площадка поле не прислала."""
    values = [o[k] for k in keys if isinstance(o.get(k), int)]
    return sum(values) if values else None


QUARTERS = {"first": 1, "second": 2, "third": 3, "fourth": 4, 1: 1, 2: 2, 3: 3, 4: 4}
SALE_TYPES = {"fz214": "ДДУ (214-ФЗ)", "dupt": "договор уступки", "pdkp": "предварительный ДКП",
              "dkp": "договор купли-продажи", "free": "свободная продажа", "alternative": "альтернатива"}


def _deadline(o: dict) -> tuple[str | None, bool | None]:
    """Срок сдачи корпуса: («4 кв. 2027», False) или («дом сдан», True).
    Точнее всего — newbuilding.house.finishDate (это «Сдача корпуса» на карточке Циана), потом building.deadline."""
    nb = o.get("newbuilding") or {}
    house = nb.get("house") or {}
    dl = house.get("finishDate") or (o.get("building") or {}).get("deadline") or {}
    finished = house.get("isFinished") if house.get("isFinished") is not None else dl.get("isComplete")
    if finished:
        return "дом сдан", True
    q, y = QUARTERS.get(dl.get("quarter")), dl.get("year")
    if y:
        return (f"{q} кв. {y}" if q else f"{y} г."), False
    return None, finished


def _from_developer(o: dict) -> bool:
    nb = o.get("newbuilding") or {}
    return bool(o.get("fromDeveloper") or nb.get("isFromDeveloper") or nb.get("isFromBuilder") or (o.get("user") or {}).get("isBuilder"))


def _developer(o: dict) -> str | None:
    """Имя застройщика Циан отдаёт только когда продаёт он сам (в выдаче у остальных — лишь id застройщика)."""
    if _from_developer(o):
        user = o.get("user") or {}
        return user.get("companyName") or user.get("agencyName")
    return None


def normalize(o: dict, city: str, today: date | None = None, new: bool = False) -> dict | None:
    """new=True — выдача новостроек: ДДУ (214-ФЗ) не отбрасываем, добавляем ЖК, срок сдачи, застройщика."""
    today = today or date.today()
    terms = o.get("bargainTerms") or {}
    price = terms.get("priceRur") or terms.get("price")
    area = _float(o.get("totalArea"))
    if not price or not area or (terms.get("saleType") == "fz214" and not new):
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
        "balconies": _count(o, "balconiesCount", "loggiasCount"),  # балконы и лоджии вместе
        "loggias": _count(o, "loggiasCount"),
        "separate_wc": _count(o, "separateWcsCount"),
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
        **(_new_fields(o, terms) if new else {"is_new": False}),
    }


# ------------------------------------------------------------------ загрузка


def _new_fields(o: dict, terms: dict) -> dict:
    nb = o.get("newbuilding") or {}
    deadline, finished = _deadline(o)
    house = nb.get('house') or {}
    date = house.get('finishDate') or (o.get('building') or {}).get('deadline') or {}
    developer = _developer(o)
    user = o.get("user") or {}
    if _from_developer(o):
        role = "застройщик"
    elif user.get("isAgent"):
        role = "агент продавца (переуступка)"
    else:
        role = "собственник (переуступка)" if terms.get("saleType") == "dupt" else "собственник"
    return {
        "is_new": True,
        "complex": nb.get("name") or None,          # ЖК
        "deadline": deadline,                       # «4 кв. 2027» / «дом сдан»
        "delivery_year": date.get('year'),
        "delivery_quarter": QUARTERS.get(date.get('quarter')),
        "finished": finished,
        "developer": developer,
        "sale_type": SALE_TYPES.get(terms.get("saleType"), terms.get("saleType")),
        "seller_role": role,
    }


async def _follow_cookie_redirects(get, url: str):
    """307 на ту же выдачу с Set-Cookie: повторяем в той же сессии, не теряя cookies.

    Саму капчу и чужие адреса не открываем. Редирект города обрабатывает _fetch_page.
    """
    global _last_request
    _last_request = time.monotonic()
    response = await get()
    for _ in range(2):
        target = redirect_target(url, response.headers.get("location", "")) if response.status_code in REDIRECTS else None
        if target != url or not response.cookies:
            break
        await asyncio.sleep(MIN_INTERVAL)
        _last_request = time.monotonic()
        response = await get()
    return response


async def _get_httpx(url: str) -> tuple[int, str, str]:
    # local_address=0.0.0.0 — принудительно IPv4: в WSL httpx иначе виснет на подключении
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
                                 headers={"Accept-Language": "ru-RU,ru;q=0.9"}) as http:
        r = await _follow_cookie_redirects(lambda: http.get(url), url)
    return r.status_code, r.headers.get("location", ""), r.text


async def _get_browser_like(url: str) -> tuple[int, str, str]:
    """Браузерные TLS-профили; редиректы по-прежнему проверяет вызывающий код."""
    from curl_cffi.requests import AsyncSession

    last = (0, "", "")
    for index, fingerprint in enumerate(("safari17_0", "chrome")):
        if index:
            await asyncio.sleep(MIN_INTERVAL)
        try:
            async with AsyncSession(impersonate=fingerprint, timeout=20) as http:
                r = await _follow_cookie_redirects(
                    lambda: http.get(url, allow_redirects=False, headers={"Accept-Language": "ru-RU,ru;q=0.9"}), url)
            last = (r.status_code, r.headers.get("location", ""), r.text)
            # Редирект на капчу — отказ профиля, а не успешная загрузка.
            # Саму капчу не открываем; второй обычный HTTP-клиент может получить выдачу.
            target = redirect_target(url, last[1]) if r.status_code in REDIRECTS else None
            if (target and target != url) or _blocked(*last) is None:
                return last
        except Exception as exc:
            log.info("Циан: профиль %s не сработал (%s)", fingerprint, type(exc).__name__)
    return last


def _blocked(status: int, location: str, text: str) -> str | None:
    if status in REDIRECTS and "captcha" in location.lower():
        return "капча"
    if status != 200:
        return f"ответ {status}"
    if not re.search(r"<title\b[^>]*>\s*[^<\s]", text, re.I):  # заглушка защиты от ботов
        return "заглушка защиты от ботов"
    return None


async def _fetch_page(city: str, rooms: int, budget: int | None, page: int, okrug: str | None = None,
                      new: bool = False) -> tuple[list[dict], str]:
    global _last_request
    url = build_url(city, rooms, budget, page, okrug, new)
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
                # Циан переводит на поддомен города (www -> kaluga.cian.ru) — идём за ним с нашими же фильтрами
                for _ in range(2):
                    target = redirect_target(url, location) if status in REDIRECTS else None
                    if not target:
                        break
                    host = urlparse(target).hostname or ""
                    if host != urlparse(url).hostname and host.endswith(".cian.ru"):
                        _learn_subdomain(city, host.split(".")[0])
                        url = build_url(city, rooms, budget, page, okrug, new)
                    else:
                        url = target
                    await asyncio.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - _last_request)))
                    _last_request = time.monotonic()
                    status, location, text = await getter(url)
            except Exception as exc:  # сеть, TLS — пробуем следующий способ
                problem = f"{name}: {type(exc).__name__}"
                continue
            problem = _blocked(status, location, text)
            if problem is None:
                break
            log.info("Циан через %s: %s — пробую дальше", name, problem)
        else:
            raise SourceUnavailable(f"Циан недоступен ({problem})")
    title = re.search(r"<title\b[^>]*>([^<]*)", text, re.I)
    if not title or REGIONS[city][2].lower() not in title.group(1).lower():
        raise SourceUnavailable("страница не про нужный город")
    raw = extract_offers(text)
    if not raw and "offers" not in text:  # страница не та (вёрстка поменялась) — а не «ничего не нашлось»
        raise SourceUnavailable("не удалось разобрать страницу")
    offers = [x for x in (normalize(o, city, new=new) for o in raw) if x]
    return offers, name


def _snapshot_path(city: str, rooms: int, new: bool = False) -> Path:
    return CACHE_DIR / f"{REGIONS[city][0]}_{rooms}{'_new' if new else ''}.json"


def _okrug_ok(x: dict, okrug: str | None) -> bool:
    return not okrug or x.get("okrug") == okrug


def _save_snapshot(city: str, rooms: int, listings: list[dict], new: bool = False) -> None:
    temporary = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _snapshot_path(city, rooms, new)
        old = load_snapshot(city, rooms, new)[0]
        merged = {x["id"]: x for x in old} | {x["id"]: x for x in listings}
        data = {"version": SNAPSHOT_VERSION, "fetched_at": datetime.now().isoformat(timespec="minutes"),
                "listings": list(merged.values())}
        # Фоновый обход и HTTP-запрос могут читать один срез: не публикуем недописанный JSON.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR, delete=False) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False)
        temporary.replace(path)
    except OSError as exc:
        log.warning("не удалось сохранить срез Циана: %s", exc)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_snapshot(city: str, rooms: int, new: bool = False) -> tuple[list[dict], str | None]:
    path = _snapshot_path(city, rooms, new)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION or not isinstance(data.get("listings"), list):
        return [], None
    listings = [x for x in data["listings"] if isinstance(x, dict) and isinstance(x.get("id"), str)
                and isinstance(x.get("price"), (int, float)) and x["price"] > 0]
    return listings, data.get("fetched_at")


async def _fetch_available(city, rooms, budget, page, okrug, new):
    """Короткий отказ повторным запросам при блокировке; срез и база остаются доступны."""
    scope = (city, new)
    failed = _failures.get(scope)
    if failed and time.monotonic() < failed[0]:
        raise SourceUnavailable(f"{failed[1]}; повторная проверка через {max(1, int(failed[0] - time.monotonic()))} с")
    try:
        result = await asyncio.wait_for(_fetch_page(city, rooms, budget, page, okrug, new), timeout=FETCH_TIMEOUT)
    except (SourceUnavailable, asyncio.TimeoutError) as exc:
        reason = str(exc) if isinstance(exc, SourceUnavailable) else "Циан не ответил за отведённое время"
        _failures[scope] = (time.monotonic() + FAILURE_COOLDOWN, reason)
        raise SourceUnavailable(reason) from None
    _failures.pop(scope, None)
    return result


async def search(city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None,
                 new: bool = False) -> tuple[list[dict], dict]:
    """Возвращает (объявления, отчёт). new — новостройки. Отчёт: status live | cached | snapshot | unavailable."""
    if not supported(city):
        raise SourceUnavailable(f"для города «{city}» площадка не подключена")
    key = (city, rooms, budget, page, okrug, new)
    hit = _memory.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1], {"status": "cached", "note": "из кеша сессии",'fetched_at':datetime.fromtimestamp(hit[0],timezone.utc).isoformat(timespec='seconds')}
    try:
        listings, via = await _fetch_available(city, rooms, budget, page, okrug, new)
        _memory[key] = (time.time(), listings)
        _save_snapshot(city, rooms, listings, new)
        note = f"страница {page}" + (" (запасной путь curl_cffi после капчи)" if via == "curl_cffi" else "")
        return listings, {"status": "live", "note": note, "via": via,'fetched_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
    except SourceUnavailable as exc:
        log.warning("Циан недоступен: %s", exc)
        if page > 1:
            raise
        saved, fetched_at = load_snapshot(city, rooms, new)
        saved = [x for x in saved if (not budget or x["price"] <= budget * 1.05) and _okrug_ok(x, okrug)]
        if saved:
            return saved, {"status": "snapshot", "note": f"{exc}; сохранённый срез от {fetched_at}",'fetched_at':fetched_at}
        raise


# ------------------------------------------------------------------ проверка страницы объявления

# Снятое объявление Циан отдаёт с кодом 200 и плашкой «Объявление снято с публикации» (проверено на живых
# страницах 29.10.2026; флаг isArchived у снятых бывает и false — на него не опираемся).
REMOVED_TEXT = re.compile(r"(?:объявление|квартира)\s+снят[оа]\s+с\s+(?:публикации|продажи)|объявление\s+удалено", re.I)
PAGE_INTERVAL = 1.0
_page_lock = asyncio.Lock()
_page_last = 0.0


def page_verdict(status: int, location: str, text: str, offer_id: str) -> tuple[bool | None, str]:
    """(в продаже ли, почему). None — не удалось понять: капча, заглушка, чужая страница, сеть."""
    if status in (404, 410):
        return False, f"страница объявления отвечает {status}"
    if status != 200:
        return None, "капча" if "captcha" in location.lower() else f"ответ {status}"
    if not re.search(r"<title>[^<]", text):
        return None, "заглушка защиты от ботов"
    if offer_id and offer_id not in text:
        return None, "на странице нет этого объявления"
    if REMOVED_TEXT.search(text):
        return False, "объявление снято с публикации"
    return True, "объявление в продаже"


async def check_listing_page(url: str | None) -> dict:
    """Открыть страницу объявления -> {"alive": True | False | None, "note", "offer"}.
    alive: True — в продаже, False — снято, None — не удалось проверить. offer — свежие данные объявления
    со страницы (как в выдаче поиска), чтобы обновить параметры: площадь, кухню, этаж, цену, описание."""
    global _page_last
    if not url or not redirect_target(url, url):  # только https-страницы Циана
        return {"alive": None, "note": "ссылка не на Циан — не открываю", "offer": None}
    offer_id = (re.search(r"/(\d{5,})/?", url) or [None, ""])[1]
    getters = [_get_httpx]
    try:
        import curl_cffi  # noqa: F401
        getters.append(_get_browser_like)
    except ImportError:
        pass
    verdict: tuple[bool | None, str] = (None, "нет ответа")
    text = ""
    for getter in getters:
        async with _page_lock:
            await asyncio.sleep(max(0.0, PAGE_INTERVAL - (time.monotonic() - _page_last)))
            _page_last = time.monotonic()
        try:
            status, location, text = await asyncio.wait_for(getter(url), timeout=15)
            target = redirect_target(url, location) if status in (301, 302, 307, 308) else None
            if target and offer_id and offer_id in target:  # то же объявление на поддомене города
                status, location, text = await asyncio.wait_for(getter(target), timeout=15)
        except Exception as exc:  # noqa: BLE001 — сеть, TLS, таймаут: пробуем следующий способ
            verdict = (None, f"{type(exc).__name__}")
            continue
        verdict = page_verdict(status, location, text, offer_id)
        if verdict[0] is not None:
            break
    return {"alive": verdict[0], "note": verdict[1], "offer": extract_offer(text) if verdict[0] else None}


async def market(city: str, rooms: int, okrug: str | None = None, new: bool = False) -> dict | None:
    """Сколько стоят такие квартиры: квартили цены по свежим объявлениям (без фильтра по бюджету)."""
    import statistics

    try:
        items, rep = await search(city, rooms, None, 1, okrug, new)
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


if __name__ == "__main__":  # прогрев: срез на диске и база агента
    import argparse

    ap = argparse.ArgumentParser(description="Прогреть объявления Циана для демо: срез на диске и база агента")
    ap.add_argument("--city", default="Казань")
    ap.add_argument("--rooms", type=int, default=2)
    ap.add_argument("--budget", type=int, default=9_000_000)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--okrug", default=None, help="для Москвы: ЮЗАО, ЦАО, …")
    ap.add_argument("--new", action="store_true", help="новостройки")
    args = ap.parse_args()

    async def main():
        from .. import db
        from . import store
        db.configure()
        total = 0
        for p in range(1, args.pages + 1):
            items, report = await search(args.city, args.rooms, args.budget, p, args.okrug, args.new)
            if report["status"] == "live":
                store.remember(items)
            total += len(items)
            print(f"страница {p}: {len(items)} объявлений ({report['status']})")
        store.track({"city": args.city, "rooms": args.rooms, "okrug": args.okrug, "budget_max": args.budget}, args.new)
        print(f"готово: {total}; срез — {_snapshot_path(args.city, args.rooms, args.new)}, объявления — в базе агента")

    asyncio.run(main())
