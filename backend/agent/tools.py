"""Инструменты агента: детерминированные расчёты, которым не нужен LLM.

Числа (цена, бюджет, платёж, риски) считает код, а не модель: так результат
воспроизводим и объясним, и модель не может «придумать» цену.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date, timedelta

from ..sources import description_flags, normalize_city

# ---------------------------------------------------------------- профиль запроса

REQUIRED = {"city": "город", "budget_max": "бюджет", "rooms": "число комнат"}

EMPTY_PROFILE = {
    "city": None,
    "rooms": None,
    "budget_max": None,
    "budget_unknown": False,
    "payment": None,          # cash | mortgage_ready | mortgage_saving
    "okrug": None,
    "districts": [],
    "near_school": False,
    "not_first_floor": False,
    "not_last_floor": False,
    "floor_any": False,
    "kitchen_min": None,
    "area_min": None,
    "area_any": False,
    "material": [],           # кирпич | монолит | панель
    "year_min": None,
    "house_any": False,
    "metro_max_minutes": None,
    "metro_any": False,
    "condition": None,        # ready | cosmetic | repair | any
    "infra": [],              # школа | садик | парк
    "infra_any": False,
    "timeline": None,         # month | quarter | exploring
    "timeline_any": False,
    "wishes": [],
}


def parse_money(text: str) -> int | None:
    """'9 млн' -> 9000000, '8,7 млн' -> 8700000, '8 700 000' -> 8700000, '8700 тыс' -> 8700000."""
    t = text.lower().replace(" ", " ")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:млн|миллион|лям)", t)
    if m:
        value = float(m.group(1).replace(",", "."))
        tail = re.search(r"(?:млн|миллион\w*)\s*(\d{1,3})\s*(?:тыс|тысяч)", t[m.start():])
        extra = int(tail.group(1)) * 1000 if tail else 0
        return int(round(value * 1_000_000)) + extra
    m = re.search(r"(\d[\d ]{2,})\s*(?:тыс|тысяч)", t)
    if m:
        return int(m.group(1).replace(" ", "")) * 1000
    m = re.search(r"\d{1,3}(?:[ .]\d{3}){2,}|\d{6,}", t)
    if m:
        return int(re.sub(r"\D", "", m.group(0)))
    return None


# Округа Москвы и как их называют люди
OKRUG_ALIASES = [
    (r"юго-?запад|\bюзао\b", "ЮЗАО"), (r"юго-?восток|\bювао\b", "ЮВАО"), (r"северо-?запад|\bсзао\b", "СЗАО"),
    (r"северо-?восток|\bсвао\b", "СВАО"), (r"\bцентр\w*|\bцао\b", "ЦАО"), (r"\bсевер\w*|\bсао\b", "САО"),
    (r"\bвосток\w*|\bвао\b", "ВАО"), (r"\bюг\b|\bюге\b|\bюаo\b|\bюао\b", "ЮАО"), (r"\bзапад\w*|\bзао\b", "ЗАО"),
]
BUDGET_UNKNOWN = r"не знаю|не уверен|сколько (?:я )?могу|сколько потян|посчита|по доход|по ипотек|в ипотеку|на ипотек|бюджет.{0,20}не|без бюджета"


def payment_from_text(text: str | None) -> str | None:
    t = (text or "").lower().replace("ё", "е")
    if re.search(r"наличн|без ипотек|вся сумма|деньги есть|всю сумму|за свои", t):
        return "cash"
    if re.search(r"коплю|накопить|копим|не хватает на взнос|нет (?:первого )?взноса|без взноса", t):
        return "mortgage_saving"
    if re.search(r"ипотек", t):
        return "mortgage_ready"
    return None


def okrug_from_text(text: str | None) -> str | None:
    t = (text or "").lower().replace("ё", "е")
    return next((name for pattern, name in OKRUG_ALIASES if re.search(pattern, t)), None)


def condition_from_text(text: str | None) -> str | None:
    """Состояние квартиры: заехать сразу | косметический | под ремонт | не важно."""
    t = (text or "").lower().replace("ё", "е")
    if re.search(r"не важ|любое состо|без разниц|не принципиаль", t):
        return "any"
    if re.search(r"заехать|готов\w* жить|сразу жить|хорош\w* ремонт|евроремонт|дизайнерск|чистов", t):
        return "ready"
    if re.search(r"косметич", t):
        return "cosmetic"
    if re.search(r"под ремонт|требует ремонта|без ремонта|без отделки|чернов|делать ремонт|ремонт своими", t):
        return "repair"
    return None


def timeline_from_text(text: str | None) -> str | None:
    """Срок покупки: month | quarter | exploring."""
    t = (text or "").lower().replace("ё", "е")
    if re.search(r"срочно|в течение месяца|через месяц|до месяца|прямо сейчас|уже нашли", t):
        return "month"
    if re.search(r"2-3 месяц|два-три месяц|полгода|квартал|к лету|к осени", t):
        return "quarter"
    if re.search(r"присматрива|не спеш|когда-нибудь|пока смотр", t):
        return "exploring"
    return None


def material_from_text(text: str | None) -> list[str]:
    t = (text or "").lower().replace("ё", "е")
    found = [m for m in ("кирпич", "монолит", "панель", "блочн", "сталин") if m in t]
    return found


def year_min_from_text(text: str | None) -> int | None:
    m = re.search(r"(?:не старше|после|новее|с)\s*(\d{4})", (text or "").lower())
    return int(m.group(1)) if m and 1900 <= int(m.group(1)) <= 2030 else None


def parse_request_rules(query: str) -> dict:
    """Запасной разбор запроса без LLM."""
    q = query.lower().replace("ё", "е")
    p = dict(EMPTY_PROFILE, districts=[], wishes=[], infra=[], material=[])
    rooms = re.search(r"(\d)\s*-?\s*(?:х\s*)?(?:к\b|комн|комнат)", q)
    if rooms:
        p["rooms"] = int(rooms.group(1))
    elif "студи" in q:
        p["rooms"] = 0
    elif "однушк" in q or "однокомн" in q:
        p["rooms"] = 1
    elif "двушк" in q or "двухкомн" in q:
        p["rooms"] = 2
    elif "трешк" in q or "трехкомн" in q:
        p["rooms"] = 3
    p["budget_max"] = parse_money(q)
    p["budget_unknown"] = p["budget_max"] is None and bool(re.search(BUDGET_UNKNOWN, q))
    p["payment"] = payment_from_text(q)
    p["city"] = normalize_city(q)
    if p["city"] == "Москва":
        p["okrug"] = okrug_from_text(q)
    p["near_school"] = "школ" in q or "ребен" in q or "дет" in q
    p["not_first_floor"] = "не перв" in q
    p["not_last_floor"] = bool(re.search(r"не последн|кроме последн", q))
    p["floor_any"] = bool(re.search(r"любой этаж|этаж не важ", q))
    p["condition"] = condition_from_text(q)
    p["metro_any"] = bool(re.search(r"метро не важ|без метро|метро не нужно", q))
    p["area_any"] = bool(re.search(r"площад\w* не важ|метраж не важ", q))
    p["house_any"] = bool(re.search(r"дом не важ|тип дома не важ", q))
    p["timeline"] = timeline_from_text(q)
    if "школ" in q:
        p["infra"].append("школа")
    if "садик" in q or "сад " in q:
        p["infra"].append("садик")
    if "парк" in q:
        p["infra"].append("парк")
    p["material"] = material_from_text(q)
    p["year_min"] = year_min_from_text(q)
    kitchen = re.search(r"кухн\w*\s*(?:от|не меньше)?\s*(\d{1,2})", q)
    if kitchen:
        p["kitchen_min"] = float(kitchen.group(1))
    area = re.search(r"(?:площад\w*|квартир\w*)\D{0,12}(?:от|не меньше)\s*(\d{2,3})\s*(?:м|кв)", q)
    if area and int(area.group(1)) >= 20:
        p["area_min"] = float(area.group(1))
    metro = re.search(r"метро\D{0,15}(\d{1,2})\s*мин", q)
    if metro:
        p["metro_max_minutes"] = int(metro.group(1))
    return p


def merge_profile(raw: dict, base: dict | None = None) -> dict:
    """Нормализует ответ LLM поверх базового профиля: пустые поля не затирают известные."""
    p = dict(base if base is not None else EMPTY_PROFILE,
             districts=[*(base or {}).get("districts", [])], wishes=[*(base or {}).get("wishes", [])],
             infra=[*(base or {}).get("infra", [])], material=[*(base or {}).get("material", [])])
    for key in EMPTY_PROFILE:
        v = raw.get(key)
        if v is None or v == "" or v == [] or v is False:  # «нет» в уточнении не отменяет сказанное раньше
            continue
        if key == "wishes" and p.get("wishes"):
            v = list(dict.fromkeys([*p["wishes"], *v]))
        p[key] = v
    p["city"] = normalize_city(p["city"]) or p["city"]
    for key, cast in (("rooms", int), ("budget_max", int), ("kitchen_min", float), ("area_min", float), ("metro_max_minutes", int)):
        try:
            p[key] = cast(p[key]) if p[key] is not None else None
        except (TypeError, ValueError):
            p[key] = None
    if p["budget_max"] is not None and p["budget_max"] < 100_000:  # «9» -> 9 млн
        p["budget_max"] = int(p["budget_max"] * 1_000_000)
    p["districts"] = list(p.get("districts") or [])
    if p.get("okrug"):  # «юго-запад» -> ЮЗАО; не Москва — округов нет
        p["okrug"] = okrug_from_text(p["okrug"]) or (p["okrug"] if p["okrug"] in ("ЦАО", "САО", "СВАО", "ВАО", "ЮВАО", "ЮАО", "ЮЗАО", "ЗАО", "СЗАО") else None)
    if p.get("city") != "Москва":
        p["okrug"] = None
    if p.get("payment") not in (None, "cash", "mortgage_ready", "mortgage_saving"):
        p["payment"] = None
    if p.get("budget_max"):
        p["budget_unknown"] = False
    if p.get("condition") not in (None, "ready", "cosmetic", "repair", "any"):
        p["condition"] = condition_from_text(str(p["condition"]))
    if p.get("timeline") not in (None, "month", "quarter", "exploring"):
        p["timeline"] = timeline_from_text(str(p["timeline"]))
    try:
        p["year_min"] = int(p["year_min"]) if p.get("year_min") else None
    except (TypeError, ValueError):
        p["year_min"] = None
    p["material"] = [str(m).lower() for m in (p.get("material") or [])][:3]
    p["infra"] = [str(i).lower() for i in (p.get("infra") or [])][:4]
    p["wishes"] = [str(w) for w in (p.get("wishes") or [])][:6]
    return p


def missing_required(p: dict) -> list[str]:
    return [k for k in REQUIRED if p.get(k) in (None, "")]


def clarify_question(missing: list[str]) -> str:
    parts = {
        "city": "в каком городе ищем",
        "budget_max": "какой максимальный бюджет",
        "rooms": "сколько нужно комнат",
    }
    asks = [parts[m] for m in missing]
    text = ", ".join(asks[:-1]) + (" и " if len(asks) > 1 else "") + asks[-1]
    return f"Чтобы начать поиск, уточните, пожалуйста: {text}?"


def mln(v: float) -> str:
    return f"{v / 1e6:.1f}".replace(".0", "").replace(".", ",") + " млн ₽"


def rooms_text(rooms: int | None) -> str:
    return "студия" if rooms == 0 else f"{rooms}-комнатные"


def wants_whole_city(text: str) -> bool:
    return bool(re.search(r"всей москв|всему город|весь город|любой округ|по всей|по всему|без округа", (text or "").lower()))


def wants_budget_help(text: str) -> bool:
    return bool(re.search(BUDGET_UNKNOWN, (text or "").lower().replace("ё", "е")))


def budget_question(p: dict, market: dict | None) -> str:
    rooms = "студии" if p["rooms"] == 0 else f"{p['rooms']}-комнатные"
    where = p["city"] + (f", {p['okrug']}" if p.get("okrug") else "")
    if not market:
        return "Какой максимальный бюджет? Если не знаете — посчитаю по вашим доходам, сколько вам доступно с ипотекой."
    return (f"Для ориентира: {rooms} на вторичке ({where}) по свежим объявлениям Циана стоят обычно "
            f"от {mln(market['p25'])} до {mln(market['p75'])}, в середине — около {mln(market['median'])} "
            f"(~{fmt(round(market['ppm2'], -3))} за м²). Какой у вас бюджет? Если не знаете — посчитаю по доходам, "
            "сколько вам доступно с ипотекой.")


def budget_chips(market: dict | None) -> list[str]:
    if not market:
        return []
    return [f"До {mln(round(v, -5))}" for v in (market["p25"], market["median"], market["p75"])]


def profile_summary(p: dict) -> str:
    rooms = "студию" if p["rooms"] == 0 else f"{p['rooms']}-комнатную квартиру"
    where = f"г. {p['city']}" + (f", {p['okrug']}" if p.get("okrug") else "")
    extra = []
    if p["near_school"]:
        extra.append("рядом со школой")
    if p["not_first_floor"]:
        extra.append("не на первом этаже")
    if p.get("not_last_floor"):
        extra.append("не на последнем этаже")
    if p.get("condition") and p["condition"] != "any":
        extra.append({"ready": "с ремонтом, чтобы заехать сразу", "cosmetic": "косметический ремонт",
                      "repair": "под ремонт"}.get(p["condition"], ""))
    if p["kitchen_min"]:
        extra.append(f"кухня от {p['kitchen_min']:g} м²")
    if p["area_min"]:
        extra.append(f"площадь от {p['area_min']:g} м²")
    if p["metro_max_minutes"]:
        extra.append(f"до метро до {p['metro_max_minutes']} мин")
    if p.get("material"):
        extra.append("дом: " + ", ".join(p["material"]))
    if p.get("year_min"):
        extra.append(f"дом не старше {p['year_min']}")
    if p.get("infra"):
        extra.append("рядом: " + ", ".join(p["infra"]))
    if p.get("timeline"):
        extra.append({"month": "срок — в течение месяца", "quarter": "срок — 2–3 месяца",
                      "exploring": "пока присматривается"}.get(p["timeline"], ""))
    tail = (", " + ", ".join(x for x in extra if x)) if extra else ""
    return f"Ищу {rooms} в {where} до {fmt(p['budget_max'])}{tail}."


# ---------------------------------------------------------------- ипотека

DEMO_RATE = 0.169  # допущение; в проде — ставка из предодобрения Сбера


def annuity(principal: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n)


def deal_costs(price: int, loan: int) -> dict:
    """Расходы на сделку (допущения по тарифам)."""
    return {
        "Оценка залога": 6_000,
        "Страхование (имущество, жизнь, титул), 1-й год": int(round(loan * 0.0095, -2)),
        "Сервис безопасных расчётов": 6_900,
        "Электронная регистрация": 2_000,
    }


def mortgage_scenarios(price: int, monthly_income: int | None = None, down_available: int | None = None) -> dict:
    costs = sum(deal_costs(price, int(price * 0.8)).values())
    variants = [("Взнос 20%, 30 лет", 0.20, 30), ("Взнос 20%, 20 лет", 0.20, 20), ("Взнос 30%, 25 лет", 0.30, 25)]
    rows = []
    for title, dp_share, years in variants:
        down = int(price * dp_share)
        loan = price - down
        pay = annuity(loan, DEMO_RATE, years)
        rows.append({
            "title": title, "down_payment": down, "loan": loan, "years": years,
            "monthly": int(round(pay, -2)),
            "overpay": int(round(pay * years * 12 - loan, -3)),
            "income_needed": int(round(pay * 2, -3)),
            "fits_income": None if monthly_income is None else pay * 2 <= monthly_income,
            "cash_needed": down + costs,
            "savings_gap": 0 if down_available is None else max(0, down + costs - down_available),
        })
    best = next((r for r in rows if r["fits_income"] is not False), rows[0])
    return {
        "rate": DEMO_RATE,
        "deal_costs": costs,
        "scenarios": rows,
        "recommended": best["title"],
        "approved": int(price * 0.8),
        "per_100k_discount": int(round(annuity(100_000, DEMO_RATE, best["years"]))),
    }


# ---------------------------------------------------------------- справедливая цена

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
    """Медиана приведённой цены м² по аналогам из выдачи × поправки объекта × поправка на торг."""
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
    args.append("покупатель с одобренной ипотекой Сбера, деньги через сервис безопасных расчётов, выход на сделку за 2–3 недели")
    return args


# ---------------------------------------------------------------- отбор и ранжирование


def passes_hard_filters(x: dict, p: dict) -> bool:
    if p.get("rooms") is not None and x["rooms"] != p["rooms"]:
        return False
    if x["price"] > p["budget_max"] * 1.05:  # чуть дороже бюджета оставляем: можно сторговать
        return False
    if p.get("not_first_floor") and x.get("floor") == 1:
        return False
    return True


def score_listing(x: dict, p: dict, fp: dict) -> float:
    value = max(-0.1, min(0.1, (fp["fair"] - x["price"]) / fp["fair"]))
    s = 30 * (0.5 + value / 0.2)
    if p.get("districts") and x.get("district") and any(d.lower()[:5] in x["district"].lower() for d in p["districts"]):
        s += 10  # район, который назвал клиент
    s += min(8, (x.get("days_on_market") or 0) / 15)
    if p.get("near_school") and re.search(r"школ", x.get("description") or "", re.I):
        s += 8
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


# ---------------------------------------------------------------- чего не хватает в объявлении

MISSING_QUESTIONS = {
    "condition": ("состояние и ремонт", "в каком состоянии квартира, какой ремонт"),
    "year": ("год постройки", "какого года дом"),
    "kitchen": ("площадь кухни", "какая площадь кухни"),
    "floors": ("этажность дома", "сколько этажей в доме"),
}
ALWAYS_ASK = [("owners", "собственники", "сколько собственников и как давно квартира в собственности")]


def missing_info(listing: dict, limit: int = 3) -> list[dict]:
    out = [{"key": k, "label": lab, "question": q} for k, (lab, q) in MISSING_QUESTIONS.items() if listing.get(k) is None]
    out += [{"key": k, "label": lab, "question": q} for k, lab, q in ALWAYS_ASK]
    return out[:limit]


# ---------------------------------------------------------------- проверка рисков


def legal_check(listing: dict) -> dict:
    """Что можно сказать по данным объявления. Выписки ЕГРН/ФССП в прототипе не запрашиваем."""
    checks = []

    def add(source, title, status, note):
        checks.append({"source": source, "title": title, "status": status, "note": note})

    rr = listing.get("rosreestr_checked")
    add(listing.get("source_name", "Площадка"), "Проверка площадкой",
        "ok" if rr else "info", "собственник проверен в Росреестре" if rr else "площадка собственника не проверяла")
    if listing.get("is_apartments"):
        add("Объявление", "Апартаменты", "warn", "не жильё: нельзя прописаться, ипотека сложнее")
    if listing.get("is_auction"):
        add("Объявление", "Продажа с торгов", "warn", "особый порядок сделки — нужен юрист")
    for note in description_flags(listing.get("description")):
        add("Описание", "Обратить внимание", "warn", note)
    add("ЕГРН, ФССП, ЕФРСБ", "Выписки по объекту и продавцу", "pending",
        "запросим после выбора объекта (в прототипе не подключено)")
    status = "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"listing_id": listing["id"], "status": status, "checks": checks}


# ---------------------------------------------------------------- просмотр и сделка

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def human_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def client_slots(today: date | None = None) -> list[str]:
    """Удобное клиенту время: ближайший будний вечер и ближайшая суббота."""
    today = today or date.today()
    weekday = next(today + timedelta(days=i) for i in range(1, 8) if (today + timedelta(days=i)).weekday() < 5)
    saturday = next(today + timedelta(days=i) for i in range(1, 8) if (today + timedelta(days=i)).weekday() == 5)
    return [f"{human_date(weekday)}, 18:30", f"{human_date(saturday)}, 11:00"]


def viewing_checklist(listing: dict) -> list[str]:
    items = [
        "Сверить паспорт продавца с выпиской ЕГРН",
        "Проверить окна, батареи и стояки",
        "Посмотреть потолки и стены на следы протечек",
        "Сверить планировку с техпланом",
        "Узнать, кто прописан и когда выпишутся",
    ]
    if listing.get("condition") in (None, "требует ремонта", "без отделки"):
        items.insert(1, "Оценить объём ремонта — от него зависит итоговая цена")
    if listing.get("floor") == 1:
        items.insert(1, "Первый этаж: проверить сырость и запах из подвала")
    for note in description_flags(listing.get("description")):
        items.append(note)
    return items


def deal_plan(listing: dict, final_price: int, down_payment: int | None, today: date | None = None) -> dict:
    """down_payment=None — покупка без ипотеки."""
    today = today or date.today()
    if down_payment is None:
        steps = [
            (0, "Цена согласована", "Итог звонка и запись разговора — в журнале сделки"),
            (2, "Выписки ЕГРН и проверка продавца", "Собственники, обременения, долги, банкротство"),
            (3, "Титульное страхование", "По желанию — защита от оспаривания сделки"),
            (5, "Подписание договора", "Электронная подпись, договор купли-продажи в Домклик"),
            (5, "Безопасные расчёты", "Деньги уходят продавцу только после регистрации"),
            (9, "Электронная регистрация", "Росреестр без визитов в МФЦ"),
        ]
        return {
            "cash": True, "loan": 0, "down_payment": final_price, "years": 0, "monthly": 0, "monthly_saved": 0,
            "costs": {"Сервис безопасных расчётов": 6_900, "Электронная регистрация": 2_000},
            "timeline": [{"date": (today + timedelta(days=d)).isoformat(), "title": t, "note": n} for d, t, n in steps],
        }
    loan = max(0, final_price - down_payment)
    years = 30
    pay = annuity(loan, DEMO_RATE, years)
    pay_before = annuity(max(0, listing["price"] - down_payment), DEMO_RATE, years)
    steps = [
        (0, "Цена согласована", "Итог звонка и запись разговора — в журнале сделки"),
        (1, "Заявка на ипотеку по объекту", f"Кредит {fmt(loan)} на {years} лет"),
        (3, "Оценка залога", "Аккредитованная оценочная компания, онлайн-заказ"),
        (4, "Выписки ЕГРН и проверка продавца", "Собственники, обременения, долги, банкротство"),
        (6, "Страхование", "Имущество, жизнь, титул — СберСтрахование"),
        (8, "Подписание договора", "Электронная подпись, договор купли-продажи в Домклик"),
        (8, "Безопасные расчёты", "Деньги уходят продавцу только после регистрации"),
        (12, "Электронная регистрация", "Росреестр без визитов в МФЦ"),
    ]
    return {
        "cash": False,
        "loan": loan,
        "down_payment": down_payment,
        "years": years,
        "monthly": int(round(pay, -2)),
        "monthly_saved": int(round(pay_before - pay, -1)),
        "costs": deal_costs(final_price, loan),
        "timeline": [{"date": (today + timedelta(days=d)).isoformat(), "title": t, "note": n} for d, t, n in steps],
    }


def fmt(v: int | float) -> str:
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
