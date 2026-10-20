"""Инструменты агента: детерминированные расчёты, которым не нужен LLM.

Числа (цена, бюджет, платёж, риски) считает код, а не модель: так результат
воспроизводим и объясним, и модель не может «придумать» цену.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, timedelta

from . import data as D

# ---------------------------------------------------------------- профиль

DEFAULT_PROFILE = {
    "city": "Казань",
    "rooms": 2,
    "budget_max": 9_000_000,
    "districts": [],
    "near_school": True,
    "not_first_floor": False,
    "savings": 2_150_000,
    "monthly_income": 230_000,
    "monthly_saving": 45_000,
    "approved_mortgage": 7_200_000,
}


def parse_money(text: str) -> int | None:
    """'9 млн' -> 9000000, '8,7 млн' -> 8700000, '8 700 000' -> 8700000, '8700 тыс' -> 8700000."""
    t = text.lower().replace(" ", " ")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:млн|миллион)", t)
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


def parse_request_rules(query: str) -> dict:
    """Запасной разбор запроса без LLM."""
    q = query.lower()
    profile = dict(DEFAULT_PROFILE)
    rooms = re.search(r"(\d)\s*-?\s*(?:к|комн)", q)
    if rooms:
        profile["rooms"] = int(rooms.group(1))
    elif "одн" in q or "однушк" in q:
        profile["rooms"] = 1
    elif "двушк" in q or "двух" in q:
        profile["rooms"] = 2
    elif "трёш" in q or "треш" in q or "трех" in q or "трёх" in q:
        profile["rooms"] = 3
    money = parse_money(q)
    if money:
        profile["budget_max"] = money
    profile["near_school"] = "школ" in q or profile["near_school"]
    profile["not_first_floor"] = "не первый" in q
    profile["districts"] = [d for d in D.DISTRICT_PPM2 if d.lower()[:6] in q]
    return profile


def merge_profile(raw: dict) -> dict:
    """Нормализует ответ LLM: неизвестные поля отбрасываются, пропуски — по умолчанию."""
    p = dict(DEFAULT_PROFILE)
    for key in DEFAULT_PROFILE:
        if key in raw and raw[key] not in (None, ""):
            p[key] = raw[key]
    p["rooms"] = int(p["rooms"])
    p["budget_max"] = int(p["budget_max"])
    p["districts"] = [d for d in (p.get("districts") or []) if d in D.DISTRICT_PPM2]
    return p


# ---------------------------------------------------------------- ипотека

DEMO_RATE = 0.169  # допущение для демо; в проде — ставка из предодобрения Сбера


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


def mortgage_scenarios(profile: dict) -> dict:
    price = profile["budget_max"]
    savings = profile["savings"]
    costs = sum(deal_costs(price, int(price * 0.8)).values())
    variants = [
        ("Взнос 20%, 30 лет", 0.20, 30),
        ("Взнос 20%, 20 лет", 0.20, 20),
        ("Взнос 30%, 25 лет", 0.30, 25),
    ]
    rows = []
    for title, dp_share, years in variants:
        down = int(price * dp_share)
        loan = price - down
        pay = annuity(loan, DEMO_RATE, years)
        need = down + costs
        gap = max(0, need - savings)
        months = -(-gap // profile["monthly_saving"]) if gap else 0
        rows.append(
            {
                "title": title,
                "down_payment": down,
                "loan": loan,
                "years": years,
                "monthly": int(round(pay, -2)),
                "overpay": int(round(pay * years * 12 - loan, -3)),
                "income_needed": int(round(pay * 2, -3)),
                "fits_income": pay * 2 <= profile["monthly_income"],
                "cash_needed": need,
                "savings_gap": gap,
                "months_to_save": months,
            }
        )
    best = next((r for r in rows if r["savings_gap"] == 0 and r["fits_income"]), rows[0])
    per_100k = annuity(100_000, DEMO_RATE, best["years"])
    return {
        "rate": DEMO_RATE,
        "approved": profile["approved_mortgage"],
        "deal_costs": costs,
        "scenarios": rows,
        "recommended": best["title"],
        "per_100k_discount": int(round(per_100k)),
    }


# ---------------------------------------------------------------- справедливая цена


def listing_factor(x: dict) -> float:
    return D.condition_factor(x["condition"]) * D.year_factor(x["year"]) * D.floor_factor(
        x["floor"], x["floors"]
    )


def fair_price(listing: dict) -> dict:
    """Модель: медиана «приведённой» цены м² по сделкам-аналогам × поправки объекта."""
    cutoff = D.TODAY - timedelta(days=180)
    comps = [
        d
        for d in D.deals()
        if d["district"] == listing["district"]
        and d["rooms"] == listing["rooms"]
        and date.fromisoformat(d["date"]) >= cutoff
    ]
    if len(comps) < 5:
        comps = [d for d in D.deals() if d["district"] == listing["district"]]
    std = []
    for d in comps:
        months = (D.TODAY - date.fromisoformat(d["date"])).days / 30
        ppm2 = d["price"] / d["area"] / listing_factor(d) * (1 + D.MONTHLY_TREND * months)
        std.append(ppm2)
    base_ppm2 = statistics.median(std)
    fair = int(round(base_ppm2 * listing_factor(listing) * listing["area"], -4))
    low, high = int(round(fair * 0.975, -4)), int(round(fair * 1.025, -4))
    target = int(round(fair * 0.985, -4))
    overprice = (listing["price"] - fair) / fair
    cheaper = sorted(
        (d for d in comps if d["price"] / d["area"] < listing["price"] / listing["area"]),
        key=lambda d: d["date"],
        reverse=True,
    )
    return {
        "fair": fair,
        "low": low,
        "high": high,
        "target": target,
        "overprice_pct": round(overprice * 100, 1),
        "ppm2": int(base_ppm2 * listing_factor(listing)),
        "comps_count": len(comps),
        "cheaper_comps": len(cheaper),
        "comps": [
            {k: d[k] for k in ("date", "area", "floor", "floors", "condition", "price")}
            for d in cheaper[:3]
        ],
        "adjustments": [
            {"name": "Состояние: " + listing["condition"], "factor": D.condition_factor(listing["condition"])},
            {"name": f"Год дома: {listing['year']}", "factor": D.year_factor(listing["year"])},
            {"name": f"Этаж {listing['floor']}/{listing['floors']}", "factor": D.floor_factor(listing["floor"], listing["floors"])},
        ],
    }


def bargaining_arguments(listing: dict, fp: dict) -> list[str]:
    args = []
    if listing["days_on_market"] >= 45:
        d = listing["days_on_market"]
        args.append(f"квартира в продаже {d} {plural(d, 'день', 'дня', 'дней')} — дольше среднего по району (~45)")
    drops = len(listing["price_history"]) - 1
    if drops > 0:
        args.append(
            f"цену уже снижали {drops} {plural(drops, 'раз', 'раза', 'раз')}: с {fmt(listing['initial_price'])} до {fmt(listing['price'])}"
        )
    if fp["overprice_pct"] > 0:
        args.append(
            f"по закрытым сделкам-аналогам справедливая цена ~{fmt(fp['fair'])} "
            f"(объявление выше на {fp['overprice_pct']}%)"
        )
    if fp["cheaper_comps"]:
        c = fp["cheaper_comps"]
        args.append(f"{c} {plural(c, 'аналог', 'аналога', 'аналогов')} в районе {plural(c, 'продан', 'проданы', 'продано')} дешевле за м²")
    for d in listing.get("defects", []):
        args.append(f"недостаток: {d}")
    args.append("покупатель с одобренной ипотекой Сбера, деньги через сервис безопасных расчётов, выход на сделку за 2 недели")
    return args


# ---------------------------------------------------------------- поиск


def score_listing(x: dict, profile: dict, fp: dict) -> float:
    value = max(-0.1, min(0.1, (fp["fair"] - x["price"]) / fp["fair"]))
    s = 30 * (0.5 + value / 0.2)
    s += min(10, x["days_on_market"] / 12) + 3 * (len(x["price_history"]) - 1)
    if profile.get("near_school"):
        m = x["school"]["minutes"]
        s += 15 if m <= 7 else 8 if m <= 10 else 0
    s += {"евроремонт": 10, "косметика": 6}.get(x["condition"], 0)
    mm = x["metro_minutes"]
    s += 8 if mm <= 10 else 5 if mm <= 15 else 2
    if x["floor"] == 1 or x["floor"] == x["floors"]:
        s -= 10 if profile.get("not_first_floor") and x["floor"] == 1 else 6
    if x["year"] < 1990:
        s -= 6
    if x.get("kitchen", 9) < 8:
        s -= 4
    s += max(0, min(5, (x["area"] - 45) / 2))
    if x["price"] > profile["budget_max"]:
        s -= 8
    return round(max(0, min(100, s)), 1)


def search(profile: dict) -> dict:
    total = len(D.listings())
    pool = [x for x in D.listings() if x["rooms"] == profile["rooms"]]
    if profile.get("districts"):
        pool = [x for x in pool if x["district"] in profile["districts"]]
    # объекты чуть дороже бюджета оставляем: их можно сторговать
    pool = [x for x in pool if x["price"] <= profile["budget_max"] * 1.03]
    if profile.get("not_first_floor"):
        pool = [x for x in pool if x["floor"] != 1]
    ranked = []
    for x in pool:
        fp = fair_price(x)
        ranked.append({"listing": public_listing(x), "fair": fp, "score": score_listing(x, profile, fp)})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return {"total": total, "matched": len(pool), "shortlist": ranked[:6]}


def public_listing(x: dict) -> dict:
    """Объявление без скрытых данных продавца (минимальная цена и т.п.)."""
    pub = {k: v for k, v in x.items() if k != "seller"}
    pub["seller_name"] = x["seller"]["name"]
    pub["seller_role"] = x["seller"]["role"]
    return pub


# ---------------------------------------------------------------- юридическая проверка


def legal_check(listing_id: str) -> dict:
    r = D.egrn().get(listing_id, {})
    checks = []

    def add(source, title, status, note):
        checks.append({"source": source, "title": title, "status": status, "note": note})

    since = date.fromisoformat(r.get("owned_since", "2000-01-01"))
    years = (D.TODAY - since).days / 365
    add("ЕГРН", "Собственники", "ok" if r.get("owners", 1) <= 2 else "info",
        f"{r.get('owners', 1)} собств., владение с {since.strftime('%d.%m.%Y')}")
    if years < 3:
        add("ЕГРН", "Срок владения", "warn", f"меньше 3 лет ({years:.1f} г.) — повышенный риск оспаривания")
    enc = r.get("encumbrance", "нет")
    add("ЕГРН", "Обременения", "info" if enc != "нет" else "ok", enc)
    add("ФССП", "Аресты и долги",
        "block" if r.get("arrest") else "ok",
        f"арест, долг {fmt(r.get('fssp_debt', 0))}" if r.get("arrest") else "не найдено")
    add("ЕФРСБ", "Банкротство продавца", "block" if r.get("bankruptcy") else "ok",
        "в процедуре банкротства" if r.get("bankruptcy") else "не найдено")
    add("Нотариат", "Доверенность", "block" if r.get("power_of_attorney") else "ok",
        "продажа по доверенности при коротком владении" if r.get("power_of_attorney") else "продаёт собственник")
    if r.get("minors"):
        add("ЕГРН", "Доли несовершеннолетних", "warn", "нужно разрешение опеки и нотариальная сделка")
    if r.get("inheritance"):
        add("ЕГРН", "Наследство", "warn", "получено по наследству < 3 лет назад — рекомендуем титульное страхование")
    redev = r.get("redevelopment", "нет")
    add("БТИ", "Перепланировка", "warn" if redev != "нет" else "ok", redev)

    statuses = {c["status"] for c in checks}
    status = "block" if "block" in statuses else "warn" if "warn" in statuses else "ok"
    return {"listing_id": listing_id, "status": status, "checks": checks}


# ---------------------------------------------------------------- план сделки


def deal_plan(listing: dict, final_price: int, profile: dict) -> dict:
    down = int(profile["budget_max"] * 0.2)
    loan = max(0, final_price - down)
    years = 30
    pay = annuity(loan, DEMO_RATE, years)
    pay_before = annuity(max(0, listing["price"] - down), DEMO_RATE, years)
    costs = deal_costs(final_price, loan)
    start = D.TODAY
    steps = [
        (0, "Цена согласована", "Итог звонка и запись разговора — в журнале сделки"),
        (1, "Заявка на ипотеку по объекту", f"Кредит {fmt(loan)} на {years} лет, предодобрение уже есть"),
        (2, "Оценка залога", "Аккредитованная оценочная компания, онлайн-заказ"),
        (3, "Страхование", "Имущество, жизнь, титул — СберСтрахование"),
        (5, "Подписание договора", "Электронная подпись, договор купли-продажи в Домклик"),
        (5, "Безопасные расчёты", "Деньги резервируются и уходят продавцу только после регистрации"),
        (9, "Электронная регистрация", "Росреестр без визитов в МФЦ"),
        (10, "Ключи и переезд", "Агент предложит переезд и ремонт — следующее «жизненное событие»"),
    ]
    return {
        "loan": loan,
        "down_payment": down,
        "years": years,
        "monthly": int(round(pay, -2)),
        "monthly_saved": int(round(pay_before - pay, -1)),
        "costs": costs,
        "timeline": [
            {"date": (start + timedelta(days=d)).isoformat(), "title": t, "note": n} for d, t, n in steps
        ],
    }


def fmt(v: int | float) -> str:
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
