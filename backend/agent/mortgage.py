"""Ипотечные программы: рыночная ипотека Сбера и семейная ипотека (условия с 01.10.2026).

Модуль отвечает на вопрос клиента «какая ипотека мне подходит и почему» и даёт
ставку, по которой дальше считаются бюджет, план накоплений и план сделки.
Числа и правила — в коде, LLM их только пересказывает.

Источники условий (проверено 21.10.2026):
- семейная ипотека с 01.10.2026: шкала ставок и лимитов по числу детей, ребёнок до 7 лет,
  вторичка только в городах, где строится не больше двух МКД, субсидия не дольше 15 лет
  (информация Минфина, КонсультантПлюс; Финуслуги);
- рыночная ставка Сбера на готовое жильё — от 15,5% с надбавкой за небольшой взнос (myfin, 06.10.2026);
- маткапитал 2026: 737 200 ₽ на первого ребёнка, 974 100 ₽ на второго, если на первого не получали.
В проде всё это заменит предодобрение Сбера и справочник программ Домклик.
"""
from __future__ import annotations

# ---------------------------------------------------------------- параметры программ

MARKET_BASE = 0.155          # «Готовое жильё» Сбера, от 15,5%
MARKET_SURCHARGES = [        # надбавка к ставке в зависимости от доли взноса
    (0.30, 0.02),            # взнос до 30% — +2 п.п.
    (0.50, 0.01),            # 30–50% — +1 п.п.
]

FAMILY_SCALE = {             # число детей -> (ставка, лимит кредита)
    "capital": {1: (0.12, 12_000_000), 2: (0.10, 15_000_000), 3: (0.08, 18_000_000),
                4: (0.06, 18_000_000), 5: (0.04, 18_000_000)},
    "regions": {1: (0.10, 6_000_000), 2: (0.08, 8_000_000), 3: (0.06, 10_000_000),
                4: (0.04, 10_000_000), 5: (0.02, 10_000_000)},
}
FAMILY_MIN_DOWN = 0.20
FAMILY_CHILD_AGE = 7
SUBSIDY_YEARS = 15
POST_SUBSIDY_RATE = 0.12     # условно: что будет со ставкой после 15 лет, решает договор с банком

CAPITAL_CITIES = {"Москва", "Санкт-Петербург"}  # + Московская и Ленинградская области
# Крупные города, где строится намного больше двух МКД: семейная на вторичку там недоступна.
BIG_CITIES = {"Москва", "Санкт-Петербург", "Казань", "Новосибирск", "Екатеринбург", "Нижний Новгород",
              "Краснодар", "Самара", "Уфа", "Ростов-на-Дону", "Пермь", "Челябинск", "Тюмень", "Воронеж"}

MATCAP_FIRST = 737_200

YEARS = 30


# ---------------------------------------------------------------- расчёты

def annuity(principal: float, annual_rate: float, years: int) -> float:
    """Ежемесячный аннуитетный платёж."""
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n)


def _balance_after(principal: float, annual_rate: float, years: int, months_paid: int) -> float:
    """Остаток долга после months_paid платежей."""
    r = annual_rate / 12
    pay = annuity(principal, annual_rate, years)
    return principal * (1 + r) ** months_paid - pay * (((1 + r) ** months_paid - 1) / r)


def _fmt(v: float) -> str:
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def _pct(rate: float) -> str:
    """0.175 -> «17,5%», 0.1 -> «10%»."""
    return f"{rate * 100:.1f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def _round_up(v: float, step: int = 100_000) -> int:
    return int(-(-v // step) * step)


def market_rate(down_share: float) -> float:
    for limit, add in MARKET_SURCHARGES:
        if down_share < limit:
            return round(MARKET_BASE + add, 4)
    return MARKET_BASE


def family_terms(city: str | None, children: int) -> tuple[float, int]:
    scale = FAMILY_SCALE["capital" if city in CAPITAL_CITIES else "regions"]
    return scale[min(max(children, 1), 5)]


def secondary_allowed(city: str | None) -> bool | None:
    """Можно ли в этом городе взять семейную ипотеку на вторичку. None — неизвестно, проверить по ЕИСЖС."""
    if not city:
        return None
    if city in CAPITAL_CITIES or city in BIG_CITIES:
        return False
    return None


def normalize_family(raw: dict | None) -> dict:
    """{children, youngest_age, matcap} -> чистые значения. matcap: сумма неизрасходованного маткапитала, ₽."""
    raw = raw or {}

    def as_int(v):
        if v is None or v == "" or isinstance(v, bool):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    children = as_int(raw.get("children"))
    age = as_int(raw.get("youngest_age"))
    matcap = raw.get("matcap")
    if matcap is True:
        matcap = MATCAP_FIRST
    matcap = as_int(matcap) or 0
    return {
        "children": children if children is None else max(0, min(children, 10)),
        "youngest_age": age if age is None else max(0, min(age, 25)),
        "matcap": max(0, min(matcap, 2_000_000)),
    }


def _loan_row(price: int, down: int, rate: float, years: int = YEARS) -> dict:
    loan = max(0, price - down)
    pay = annuity(loan, rate, years)
    return {
        "down_payment": int(down), "loan": int(loan), "rate": rate, "years": years,
        "monthly": int(round(pay, -2)),
        "overpay": int(round(pay * years * 12 - loan, -3)),
    }


def _family_after_subsidy(row: dict) -> int:
    """Платёж после окончания субсидии (условная ставка), чтобы клиент не удивился через 15 лет."""
    left = _balance_after(row["loan"], row["rate"], row["years"], SUBSIDY_YEARS * 12)
    return int(round(annuity(left, POST_SUBSIDY_RATE, row["years"] - SUBSIDY_YEARS), -2))


def programs(price: int, city: str | None, family: dict | None, *, down_share: float = 0.2,
             is_secondary: bool = True, secondary_ok: bool | None = None, housing: str | None = None) -> dict:
    """Какие программы подходят для квартиры за price в городе city.

    housing — что просил клиент: "secondary" / "new" / "both" / None (не сказал); если не сказал,
    расчёт для вторички, и агент говорит это допущение вслух.
    secondary_ok — можно ли в городе семейную на вторичку; None — определить по городу (secondary_allowed).

    Возвращает карточки программ со статусом ok / no / check, причинами простыми словами,
    платежами и итоговым выбором (по нему считаются бюджет, план накоплений и сделка).
    """
    fam = normalize_family(family)
    if secondary_ok is None:
        secondary_ok = secondary_allowed(city)
    down = int(round(price * down_share, -3))
    matcap = fam["matcap"]

    # 1. Рыночная — доступна всегда (при одобрении банком)
    m_rate = market_rate(down_share)
    market = {"key": "market", "title": "Рыночная ипотека Сбера", "status": "ok",
              **_loan_row(price, down, m_rate),
              "limit": None,
              "reasons": [f"Ставка от {_pct(MARKET_BASE)}; при взносе меньше 30% — {_pct(m_rate)}. "
                          "Итоговую ставку даст предодобрение Сбера."]}

    # 2. Семейная — проверяем условия по порядку
    children, age = fam["children"], fam["youngest_age"]
    reasons: list[str] = []
    child_ok: bool | None
    if children is None:
        child_ok = None
        reasons.append("Есть ли в семье ребёнок младше 7 лет? От этого зависит, подходит ли семейная ипотека.")
    elif children == 0:
        child_ok = False
        reasons.append("Семейная ипотека — для семей с ребёнком младше 7 лет (или ребёнком с инвалидностью).")
    elif age is None:
        child_ok = None
        reasons.append("Нужен возраст младшего ребёнка: программа действует, если ему меньше 7 лет на дату договора.")
    elif age >= FAMILY_CHILD_AGE:
        child_ok = False
        reasons.append(f"Младшему ребёнку {age} — а с 01.10.2026 нужен ребёнок младше 7 лет. "
                       "Основание «двое несовершеннолетних детей» для новых договоров отменено.")
    else:
        child_ok = True

    f_rate, f_limit = family_terms(city, children or 1)
    family_card = {"key": "family", "title": "Семейная ипотека", "limit": f_limit,
                   "subsidy_years": SUBSIDY_YEARS, **_loan_row(price, down, f_rate)}
    alt = None  # «если рассмотреть новостройку»

    if child_ok is True:
        reasons.append(f"{children} {'ребёнок' if children == 1 else 'детей'}, младшему {age}: "
                       f"ставка {_pct(f_rate)}, лимит кредита {_fmt(f_limit)}"
                       f"{' (Москва и Петербург)' if city in CAPITAL_CITIES else ''}.")
        place_ok = True
        place_reason = None
        if is_secondary:
            if city in CAPITAL_CITIES:
                place_ok = False
                place_reason = "На вторичку в Москве, Петербурге и их областях семейная ипотека не выдаётся — только новостройки и ИЖС."
                reasons.append(place_reason)
            elif secondary_ok is False:
                place_ok = False
                place_reason = (f"На вторичку программа действует только в городах, где строится не больше двух "
                                f"многоквартирных домов. В городе {city} строится намного больше — поэтому только новостройки.")
                reasons.append(place_reason)
            elif secondary_ok is None:
                place_ok = None
                reasons.append("На вторичку — только если в городе строится не больше двух МКД и дому не больше 20 лет. "
                               "Проверю по реестру ЕИСЖС перед заявкой.")
            else:
                reasons.append("Вторичка подходит: в городе почти не строят, дому должно быть не больше 20 лет.")
        # лимит кредита
        need_down = max(down, price - f_limit, int(price * FAMILY_MIN_DOWN))
        need_down = _round_up(need_down, 10_000)
        limit_ok = family_card["loan"] <= f_limit
        limit_reason = None
        if not limit_ok:
            limit_reason = (f"Кредит {_fmt(family_card['loan'])} больше лимита {_fmt(f_limit)}: "
                            f"нужен взнос от {_fmt(need_down)} вместо {_fmt(down)}.")
            reasons.append(limit_reason)
        family_card["main_reason"] = place_reason if place_ok is False else limit_reason
        if place_ok is True and limit_ok:
            status = "ok"
        elif place_ok is False or not limit_ok:
            status = "no"
        else:
            status = "check"
        if status == "no" and place_ok is not False:  # мешает только лимит — покажем вариант с большим взносом
            family_card.update(_loan_row(price, need_down, f_rate))
            family_card["needs_down"] = need_down
        if place_ok is False:  # сравнение с новостройкой — хороший аргумент для клиента и для Домклик
            alt = {"key": "family_new", "title": "Семейная — если рассмотреть новостройку", "status": "info",
                   "limit": f_limit, "subsidy_years": SUBSIDY_YEARS, **_loan_row(price, need_down, f_rate),
                   "reasons": [f"Та же цена {_fmt(price)}, взнос {_fmt(need_down)}, ставка {_pct(f_rate)} первые {SUBSIDY_YEARS} лет. "
                               "Могу искать и в новостройках — спрошу, где искать."]}
            alt["monthly_after_subsidy"] = _family_after_subsidy(alt)
        family_card["monthly_after_subsidy"] = _family_after_subsidy(family_card)
    else:
        status = "check" if child_ok is None else "no"
        family_card["main_reason"] = reasons[0]
    family_card["status"] = status
    family_card["reasons"] = reasons
    # платёж показываем, только если он реален: программа подходит, нужно уточнить или мешает лишь лимит (с бо́льшим взносом)
    family_card["show_payment"] = child_ok is True and (status in ("ok", "check") or bool(family_card.get("needs_down")))
    market["show_payment"] = True
    if alt:
        alt["show_payment"] = True

    cards = [market, family_card] + ([alt] if alt else [])
    chosen = family_card if family_card["status"] == "ok" else market

    # 3. Маткапитал — в первоначальный взнос (работает с любой программой)
    mc = None
    if matcap:
        mc = {"amount": matcap, "own_down": max(0, chosen["down_payment"] - matcap),
              "note": "Маткапитал можно направить на первоначальный взнос или в погашение долга. "
                      "После покупки нужно выделить доли детям — это обязательство перед Соцфондом."}

    return {
        "family": fam, "city": city, "price": price, "secondary": is_secondary,
        "programs": cards, "chosen": chosen["key"], "rate": chosen["rate"],
        "housing": housing,
        "matcap": mc, "explanation": explain(cards, chosen, mc, housing),
        "assumption": "Ставки — по открытым данным на октябрь 2026; итог даст предодобрение Сбера.",
    }


def explain(cards: list[dict], chosen: dict, mc: dict | None, housing: str | None = None) -> str:
    """Короткое объяснение для чата: что подходит, что нет и почему, сколько это в рублях в месяц.

    Тип жилья клиент мог не назвать — тогда говорим вслух, что ищем на вторичке, а не додумываем молча.
    """
    by = {c["key"]: c for c in cards}
    market, family, alt = by["market"], by["family"], by.get("family_new")
    parts = []
    if housing == "new":
        parts.append("Считаю для новостройки.")
    elif housing is None and alt:
        # тип жилья не назван, а от него зависит семейная ипотека — говорим допущение и предложим выбор
        parts.append("Считаю для вторичного рынка — вы не сказали, новостройка или вторичка.")
    if family["status"] == "ok":
        diff = market["monthly"] - family["monthly"]
        parts.append(f"Вам подходит семейная ипотека: {_pct(family['rate'])} вместо {_pct(market['rate'])} по рыночной — "
                     f"платёж {_fmt(family['monthly'])} вместо {_fmt(market['monthly'])}, на {_fmt(diff)} в месяц меньше. "
                     f"Льготная ставка действует {SUBSIDY_YEARS} лет, дальше условия по договору "
                     f"(при 12% платёж был бы около {_fmt(family['monthly_after_subsidy'])}).")
    elif family["status"] == "no":
        why = family.get("main_reason") or ""
        parts.append(f"Семейная ипотека здесь не подходит. {why}")
        if alt:
            parts.append(f"Для сравнения: в новостройке по семейной платёж был бы {_fmt(alt['monthly'])} "
                         f"при взносе {_fmt(alt['down_payment'])}.")
        elif family.get("needs_down"):
            parts.append(f"С таким взносом платёж по семейной был бы {_fmt(family['monthly'])}.")
        parts.append(f"Считаю по рыночной ставке {_pct(market['rate'])}: платёж {_fmt(market['monthly'])}.")
    else:
        parts.append(" ".join(family["reasons"]))
        parts.append(f"Пока считаю по рыночной ставке {_pct(market['rate'])}: платёж {_fmt(market['monthly'])}.")
    if mc:
        parts.append(f"Маткапитал {_fmt(mc['amount'])} пойдёт во взнос — из своих нужно {_fmt(mc['own_down'])}.")
    return " ".join(parts)


def listing_payment(listing: dict, profile: dict, price: int | None = None, down_share: float = 0.2) -> dict:
    """Ипотека для конкретной квартиры: новостройка — семейная, если подходит; вторичка — по правилам города.

    Для карточки подборки и плана сделки: программа, ставка, платёж и подсказка, если семейную
    закрывает только лимит («семейная — при взносе от …»).
    """
    price = price or listing["price"]
    r = programs(price, listing.get("city") or profile.get("city"), profile, down_share=down_share,
                 is_secondary=not listing.get("is_new"), housing="new" if listing.get("is_new") else "secondary")
    cards = {c["key"]: c for c in r["programs"]}
    chosen = cards[r["chosen"]]
    fam = cards["family"]
    hint = None
    if r["chosen"] == "market" and fam["status"] == "no" and fam.get("needs_down"):
        hint = f"семейная — при взносе от {_fmt(fam['needs_down'])}"
    elif r["chosen"] == "family" and "уступк" in (listing.get("sale_type") or ""):
        hint = "переуступка — банк проверит договор по условиям программы"
    return {"program": "семейная" if r["chosen"] == "family" else "рыночная", "title": chosen["title"],
            "rate": chosen["rate"], "monthly": chosen["monthly"], "down": chosen["down_payment"], "hint": hint}


def max_family_price(city: str | None, family: dict | None, down_share: float = 0.2) -> int | None:
    """До какой цены квартиры проходит семейная при взносе down_share (лимит кредита / (1 − взнос))."""
    fam = normalize_family(family)
    if not fam["children"] or fam["youngest_age"] is None or fam["youngest_age"] >= FAMILY_CHILD_AGE:
        return None
    _, limit = family_terms(city, fam["children"])
    return int(limit / (1 - down_share) // 100_000 * 100_000)


# ---------------------------------------------------------------- сценарии и расходы

DEMO_RATE = market_rate(0.2)  # рыночная ставка Сбера при взносе 20%; в проде — из предодобрения


def deal_costs(price: int, loan: int) -> dict:
    """Расходы на сделку (допущения по тарифам)."""
    return {
        "Оценка залога": 6_000,
        "Страхование (имущество, жизнь, титул), 1-й год": int(round(loan * 0.0095, -2)),
        "Сервис безопасных расчётов": 6_900,
        "Электронная регистрация": 2_000,
    }


def mortgage_scenarios(price: int, monthly_income: int | None = None, down_available: int | None = None,
                       rate: float | None = None, program: str | None = None, matcap: int = 0) -> dict:
    """Три сценария ипотеки. rate=None — рыночная ставка Сбера (зависит от взноса), иначе ставка программы.
    matcap — маткапитал: идёт во взнос, своих нужно меньше."""
    costs = sum(deal_costs(price, int(price * 0.8)).values())
    variants = [("Взнос 20%, 30 лет", 0.20, 30), ("Взнос 20%, 20 лет", 0.20, 20), ("Взнос 30%, 25 лет", 0.30, 25)]
    rows = []
    for title, dp_share, years in variants:
        down = int(price * dp_share)
        loan = price - down
        r = rate if rate is not None else market_rate(dp_share)
        pay = annuity(loan, r, years)
        rows.append({
            "title": title, "down_payment": down, "loan": loan, "years": years, "rate": r,
            "monthly": int(round(pay, -2)),
            "overpay": int(round(pay * years * 12 - loan, -3)),
            "income_needed": int(round(pay * 2, -3)),
            "fits_income": None if monthly_income is None else pay * 2 <= monthly_income,
            "cash_needed": max(0, down - matcap) + costs,
            "savings_gap": 0 if down_available is None else max(0, down - matcap + costs - down_available),
        })
    best = next((r for r in rows if r["fits_income"] is not False), rows[0])
    return {
        "rate": best["rate"],
        "program": program or "Рыночная ипотека Сбера",
        "matcap": matcap,
        "deal_costs": costs,
        "scenarios": rows,
        "recommended": best["title"],
        "approved": int(price * 0.8),
        "per_100k_discount": int(round(annuity(100_000, best["rate"], best["years"]))),
    }
