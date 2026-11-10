"""Демо-расчёт ипотечных программ по версионированным допущениям.

Формулы работают с backend/calculation_policy.json. Условия не являются
подтверждённой линейкой банковских продуктов; реальные условия даёт банк.
"""
from __future__ import annotations

from ..calculation_policy import POLICY, metadata

MARKET_BASE = POLICY["mortgage"]["market_base"]
MARKET_SURCHARGES = POLICY["mortgage"]["market_surcharges"]
FAMILY_MIN_DOWN = POLICY["mortgage"]["family_min_down"]
FAMILY_CHILD_AGE = POLICY["mortgage"]["family_child_age"]
SUBSIDY_YEARS = POLICY["mortgage"]["subsidy_years"]
POST_SUBSIDY_RATE = POLICY["mortgage"]["post_subsidy_rate"]
CAPITAL_CITIES = POLICY["mortgage"]["capital_cities"]
BIG_CITIES = POLICY["mortgage"]["big_cities"]
MATCAP_FIRST = POLICY["mortgage"]["matcap_first"]
YEARS = POLICY["mortgage"]["years"]
DEFAULT_DOWN_SHARE = POLICY["mortgage"]["default_down_share"]
FAMILY_SCALE = {region: {int(children): tuple(terms) for children, terms in scale.items()}
                for region, scale in POLICY['mortgage']['family_scale'].items()}


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


def profile_down_payment(price: int, profile: dict) -> int | None:
    """Наличные и маткапитал; неизвестные наличные не заменяются долей цены."""
    cash=profile.get('deposit')
    if cash is None:return None
    return min(price,max(0,int(cash))+normalize_family(profile)['matcap'])


def programs(price: int, city: str | None, family: dict | None, *, down_share: float = DEFAULT_DOWN_SHARE,
             down_payment: int | None = None,
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
    down = int(round(price * down_share, -3)) if down_payment is None else min(price,max(0,int(down_payment)))
    down_share = down / price
    matcap = min(fam["matcap"],down)

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
        reasons.append(f"Есть ли в семье ребёнок младше {FAMILY_CHILD_AGE} лет? От этого зависит, подходит ли семейная ипотека.")
    elif children == 0:
        child_ok = False
        reasons.append(f"Семейная ипотека — для семей с ребёнком младше {FAMILY_CHILD_AGE} лет (или ребёнком с инвалидностью).")
    elif age is None:
        child_ok = None
        reasons.append(f"Нужен возраст младшего ребёнка: программа действует, если ему меньше {FAMILY_CHILD_AGE} лет на дату договора.")
    elif age >= FAMILY_CHILD_AGE:
        child_ok = False
        reasons.append(f"Младшему ребёнку {age} — в демо-расчёте нужен ребёнок младше {FAMILY_CHILD_AGE} лет. Условия программы уточнит банк.")
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
        down_ok = down >= int(price * FAMILY_MIN_DOWN)
        down_reason = None
        if not down_ok:
            down_reason=f"Для семейной нужен взнос от {_fmt(need_down)}; сейчас {_fmt(down)}."
            reasons.append(down_reason)
        family_card["main_reason"] = place_reason if place_ok is False else limit_reason or down_reason
        if place_ok is True and limit_ok and down_ok:
            status = "ok"
        elif place_ok is False or not limit_ok or not down_ok:
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
        "assumptions": metadata(), "family": fam, "city": city, "price": price, "secondary": is_secondary,
        "programs": cards, "chosen": chosen["key"], "rate": chosen["rate"],
        "housing": housing,
        "matcap": mc, "explanation": explain(cards, chosen, mc, housing),
        "assumption": POLICY['note'],
        "subsidy_years": SUBSIDY_YEARS, "post_subsidy_rate": POST_SUBSIDY_RATE,
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
                     f"(при {_pct(POST_SUBSIDY_RATE)} платёж был бы около {_fmt(family['monthly_after_subsidy'])}).")
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


def listing_payment(listing: dict, profile: dict, price: int | None = None, down_share: float | None = None) -> dict | None:
    """Ипотека для конкретной квартиры: новостройка — семейная, если подходит; вторичка — по правилам города.

    Для карточки подборки и плана сделки: программа, ставка, платёж и подсказка, если семейную
    закрывает только лимит («семейная — при взносе от …»).
    Если наличные неизвестны, расчёт откладывается; down_share задаёт явное допущение для сравнения.
    """
    price = price or listing["price"]
    down = profile_down_payment(price,profile)
    if down is None and down_share is None:return None
    r = programs(price, listing.get("city") or profile.get("city"), profile,
                 down_share=down_share if down_share is not None else DEFAULT_DOWN_SHARE,down_payment=down,
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


def max_family_price(city: str | None, family: dict | None, down_share: float = DEFAULT_DOWN_SHARE) -> int | None:
    """До какой цены квартиры проходит семейная при взносе down_share (лимит кредита / (1 − взнос))."""
    fam = normalize_family(family)
    if not fam["children"] or fam["youngest_age"] is None or fam["youngest_age"] >= FAMILY_CHILD_AGE:
        return None
    _, limit = family_terms(city, fam["children"])
    return int(limit / (1 - down_share) // 100_000 * 100_000)


# ---------------------------------------------------------------- сценарии и расходы

DEMO_RATE = market_rate(DEFAULT_DOWN_SHARE)  # рыночная ставка Сбера при взносе 20%; в проде — из предодобрения


def deal_costs(price: int, loan: int) -> dict:
    """Расходы на сделку (допущения по тарифам)."""
    costs = POLICY['mortgage']['costs']
    return {
        "Оценка залога": costs['appraisal'],
        "Страхование (имущество, жизнь, титул), 1-й год": int(round(loan * costs['insurance_rate'], -2)),
        "Сервис безопасных расчётов": costs['settlement'],
        "Электронная регистрация": costs['registration'],
    }


def mortgage_scenarios(price: int, monthly_income: int | None = None, down_available: int | None = None,
                       rate: float | None = None, program: str | None = None, matcap: int = 0,
                       down_payment: int | None = None) -> dict:
    """Фактический взнос (если указан) и три сравнительных сценария ипотеки.

    rate=None — рыночная ставка Сбера (зависит от взноса), иначе ставка программы.
    matcap входит во взнос: собственных денег нужно меньше.
    """
    variants = [(f"Взнос {_pct(share)}, {years} лет", int(price*share), years) for share, years in POLICY["mortgage"]["scenarios"]]
    if down_payment is not None:
        variants=[('Ваш первоначальный взнос',min(price,max(0,int(down_payment))),YEARS),
                  *[('Для сравнения: '+title,down,years) for title,down,years in variants]]
    rows = []
    for title, down, years in variants:
        dp_share=down/price
        loan = price - down
        costs = sum(deal_costs(price,loan).values())
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
    own = rows[0] if down_payment is not None else None
    if own:
        # Варианты для сравнения требуют других денег: показываем разницу с вашим, а не «лучший» вариант.
        own["own"] = True
        for r in rows[1:]:
            r["vs_own"] = {"down": r["down_payment"] - own["down_payment"], "monthly": r["monthly"] - own["monthly"],
                           "overpay": r["overpay"] - own["overpay"]}
    best = own or next((r for r in rows if r["fits_income"] is not False), rows[0])
    # Рекомендуем только то, что подтверждено доходом клиента; свой взнос — это расчёт, а не рекомендация.
    fitting = None if own else next((r for r in rows if r["fits_income"] is True), None)
    return {
        "rate": best["rate"],
        "program": program or "Рыночная ипотека Сбера",
        "matcap": matcap,
        "deal_costs": sum(deal_costs(price,best['loan']).values()),
        "scenarios": rows,
        "recommended": fitting["title"] if fitting else None,
        "approved": best['loan'],
        "per_100k_discount": int(round(annuity(100_000, best["rate"], best["years"]))),
    }
