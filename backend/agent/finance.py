"""Финансовый помощник: стратегия накопления первоначального взноса.

По доходам, расходам и накоплениям считает, сколько не хватает на взнос и
расходы по сделке, и показывает сценарии накопления на продуктах Сбера
(накопительный счёт, вклад, облигации и фонды акций в СберИнвестициях)
для трёх уровней риска.

Ставки и доходности — допущения для демо, не обещание дохода. В проде их
подставит актуальная линейка продуктов, а риск-профиль — анкета брокера.
"""
from __future__ import annotations

from datetime import date

from .mortgage import deal_costs
from .numbers import fmt

PRODUCTS = {
    "savings_account": {"name": "Накопительный счёт", "rate": 0.10, "low": 0.10, "high": 0.10,
                        "note": "снять можно в любой момент"},
    "deposit": {"name": "Вклад на 6–12 месяцев", "rate": 0.12, "low": 0.12, "high": 0.12,
                "note": "ставка фиксирована, застрахован АСВ"},
    "bonds": {"name": "Облигации (СберИнвестиции)", "rate": 0.13, "low": 0.09, "high": 0.16,
              "note": "доход выше вклада, цена может колебаться"},
    "equity": {"name": "Фонд акций (СберИнвестиции)", "rate": 0.16, "low": -0.10, "high": 0.30,
               "note": "высокий потенциал и высокий риск"},
}

PROFILES = {
    "conservative": {"title": "Консервативная", "gen": "консервативной", "mix": {"savings_account": 0.4, "deposit": 0.6}},
    "moderate": {"title": "Умеренная", "gen": "умеренной", "mix": {"savings_account": 0.2, "deposit": 0.5, "bonds": 0.3}},
    "aggressive": {"title": "Агрессивная", "gen": "агрессивной", "mix": {"savings_account": 0.1, "deposit": 0.3, "bonds": 0.35, "equity": 0.25}},
}
AGGRESSIVE_MIN_MONTHS = 24  # акции на коротком сроке — риск не успеть к сделке

DEFAULT_INPUTS = {
    "monthly_income": 250_000,
    "monthly_expenses": 120_000,
    "savings": 1_500_000,
    "horizon_months": 12,
    "risk": "moderate",
}

CUSHION_MONTHS = 3      # подушка безопасности — не тратим на взнос
CONTRIBUTION_SHARE = 0.7  # от свободных денег в месяц откладываем 70%
DOWN_SHARE = 0.2


def _rates(mix: dict) -> tuple[float, float, float]:
    exp = sum(PRODUCTS[k]["rate"] * w for k, w in mix.items())
    low = sum(PRODUCTS[k]["low"] * w for k, w in mix.items())
    high = sum(PRODUCTS[k]["high"] * w for k, w in mix.items())
    return exp, low, high


def _months_to(target: float, start: float, monthly: float, rate: float, cap: int = 120) -> int | None:
    bal, r = start, rate / 12
    for m in range(cap + 1):
        if bal >= target:
            return m
        bal = bal * (1 + r) + monthly
    return None


def _series(start: float, monthly: float, rate: float, months: int) -> list[int]:
    out, bal, r = [], start, rate / 12
    for _ in range(months + 1):
        out.append(int(round(bal, -3)))
        bal = bal * (1 + r) + monthly
    return out


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def plan(price: int, inputs: dict, today: date | None = None, matcap: int = 0) -> dict:
    """matcap — маткапитал: идёт в первоначальный взнос, копить нужно меньше."""
    today = today or date.today()
    i = {**DEFAULT_INPUTS, **{k: v for k, v in inputs.items() if v not in (None, "")}}
    income, expenses = int(i["monthly_income"]), int(i["monthly_expenses"])
    savings, horizon = int(i["savings"]), max(1, int(i["horizon_months"]))
    risk = i["risk"] if i["risk"] in PROFILES else "moderate"

    down = int(price * DOWN_SHARE)
    costs = sum(deal_costs(price, price - down).values())
    matcap = max(0, min(int(matcap or 0), down))
    need = down - matcap + costs
    cushion = expenses * CUSHION_MONTHS
    start = max(0, savings - cushion)
    free = max(0, income - expenses)
    monthly = int(round(free * CONTRIBUTION_SHARE, -3))
    gap = max(0, need - start)

    note = None
    if risk == "aggressive" and horizon < AGGRESSIVE_MIN_MONTHS:
        note = f"На сроке меньше {AGGRESSIVE_MIN_MONTHS} мес. акции могут просесть к моменту сделки — советую умеренную стратегию."

    span = max(horizon, 3)
    strategies = []
    for key, prof in PROFILES.items():
        exp, low, high = _rates(prof["mix"])
        m_exp = _months_to(need, start, monthly, exp)
        span = max(span, min(m_exp or 36, 36))
        strategies.append({
            "key": key, "title": prof["title"], "gen": prof["gen"], "rate": round(exp, 4), "low": round(low, 4), "high": round(high, 4),
            "months": m_exp,
            "months_worst": _months_to(need, start, monthly, low),
            "mix": [{"product": k, "name": PRODUCTS[k]["name"], "share": w, "rate": PRODUCTS[k]["rate"],
                     "note": PRODUCTS[k]["note"]} for k, w in prof["mix"].items()],
        })
    span = min(span + 2, 48)
    for s in strategies:
        s["series"] = _series(start, monthly, s["rate"], span)
        s["series_low"] = _series(start, monthly, s["low"], span)
        s["series_high"] = _series(start, monthly, s["high"], span)
        s["balance_at_horizon"] = s["series"][min(horizon, span)]

    chosen = next(s for s in strategies if s["key"] == risk)
    exp_rate = chosen["rate"]
    r = exp_rate / 12
    # сколько откладывать в месяц, чтобы успеть к желаемому сроку
    fv_start = start * (1 + r) ** horizon
    annuity_factor = ((1 + r) ** horizon - 1) / r if r else horizon
    monthly_needed = max(0, int(round((need - fv_start) / annuity_factor, -3)))

    months = chosen["months"] if gap else 0
    ready_date = _add_months(today, months) if months is not None else None
    allocation = [
        {**m, "amount_now": int(round(start * m["share"], -3)), "monthly": int(round(monthly * m["share"], -3))}
        for m in chosen["mix"]
    ]
    # пока ищем квартиру (~2 мес.), деньги на взнос работают
    search_income = int(round(start * exp_rate / 12 * 2, -2))

    if not gap:
        verdict = f"На взнос и расходы по сделке уже хватает. Пока ищем квартиру, деньги по {chosen['gen']} стратегии принесут ~{fmt(search_income)} за 2 месяца."
    elif months is None:
        verdict = "С текущими пополнениями накопить на взнос не получится — нужно снизить бюджет или увеличить пополнения."
    else:
        verdict = (f"Не хватает {fmt(gap)}. Откладывая {fmt(monthly)} в месяц, по {chosen['gen']} стратегии "
                   f"накопите за {months} мес. — к {ready_date.strftime('%m.%Y')}.")

    return {
        "inputs": {"monthly_income": income, "monthly_expenses": expenses, "savings": savings,
                   "horizon_months": horizon, "risk": risk},
        "price": price, "down_payment": down, "deal_costs": costs, "need": need, "matcap": matcap,
        "cushion": cushion, "available": start, "gap": gap,
        "free_monthly": free, "monthly": monthly, "monthly_needed": monthly_needed,
        "months": months, "ready_date": ready_date.isoformat() if ready_date else None,
        "fits_horizon": months is not None and months <= horizon,
        "risk": risk, "risk_note": note, "strategies": strategies, "allocation": allocation,
        "search_income": search_income, "verdict": verdict,
        "assumption": "Ставки и доходности — допущения для демо, не обещание дохода.",
    }


def affordable(inputs: dict, rate: float, years: int = 30) -> dict:
    """Сколько клиент может себе позволить с ипотекой.

    Два ограничения: платёж не больше половины дохода и не больше свободных денег
    (как банк считает долговую нагрузку), и первый взнос 20% + расходы по сделке
    из накоплений (за вычетом подушки) плюс пополнений к желаемому сроку.
    """
    i = {**DEFAULT_INPUTS, **{k: v for k, v in inputs.items() if v not in (None, "")}}
    income, expenses = int(i["monthly_income"]), int(i["monthly_expenses"])
    savings, horizon = int(i["savings"]), max(1, int(i["horizon_months"]))
    risk = i["risk"] if i["risk"] in PROFILES else "moderate"
    exp_rate = _rates(PROFILES[risk]["mix"])[0]

    start = max(0, savings - expenses * CUSHION_MONTHS)
    monthly = int(round(max(0, income - expenses) * CONTRIBUTION_SHARE, -3))
    available = _series(start, monthly, exp_rate, horizon)[-1]

    payment_limit = min(income * 0.5, max(0, income - expenses))
    r = rate / 12
    per_ruble = r / (1 - (1 + r) ** (-years * 12))
    by_payment = payment_limit / per_ruble / (1 - DOWN_SHARE)
    # взнос 20% + страховка ~0,95% от кредита + фиксированные расходы ~15 тыс.
    by_down = max(0, available - 14_900) / (DOWN_SHARE + 0.0095 * (1 - DOWN_SHARE))
    budget = int(min(by_payment, by_down) // 100_000 * 100_000)
    return {
        "budget": budget,
        "by_payment": int(round(by_payment, -5)),
        "by_down": int(round(by_down, -5)),
        "limited_by": "payment" if by_payment < by_down else "down_payment",
        "payment_limit": int(round(payment_limit, -3)),
        "available_at_horizon": int(round(available, -3)),
        "horizon_months": horizon,
        "inputs": {"monthly_income": income, "monthly_expenses": expenses, "savings": savings,
                   "horizon_months": horizon, "risk": risk},
    }


def bank_value(fin: dict, opened: dict[str, int]) -> list[dict]:
    """Оценка дохода Сбера от открытых продуктов за год (маржа на привлечённых средствах, комиссии)."""
    margin = {"savings_account": 0.02, "deposit": 0.02, "bonds": 0.008, "equity": 0.015}
    return [
        {"name": PRODUCTS[k]["name"], "amount": amount, "value": int(round(amount * margin[k], -2))}
        for k, amount in opened.items() if k in PRODUCTS
    ]
