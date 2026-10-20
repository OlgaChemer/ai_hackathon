"""Юнит-экономика сделки и ROI для Сбера (формулы из бизнес-модели, все числа — входы).

Финансовый помощник добавляет второй поток: клиенты в воронке (в том числе те,
кто так и не купил) открывают вклады и инвестиции, чтобы собрать взнос.
Банк зарабатывает маржу на привлечённых средствах и комиссии брокера.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass
class Inputs:
    price: int = 9_000_000            # цена квартиры
    ltv: float = 0.8                  # доля ипотеки
    savings_pct: float = 0.03         # экономия на торге
    fee_pct: float = 0.15             # плата за результат от экономии
    mortgage_margin_pct: float = 0.03  # маржа по ипотеке за срок жизни кредита, от суммы
    incrementality: float = 0.30      # доля сделок, которые без агента не пришли бы в Сбер
    insurance: int = 15_000           # доп. комиссия по страхованию на сделку
    services: int = 10_000            # СБР, электронная регистрация, проверка
    manager_hours: float = 4
    manager_rate: int = 1_500
    case_cost: int = 8_000            # переменные затраты на начатый кейс
    conversion: float = 0.25          # кейс -> сделка
    reserve: int = 3_000              # резерв на ошибки агента
    deals: int = 2_000                # закрытых сделок в год
    fixed_costs: int = 150_000_000    # постоянные затраты в год
    # финансовый помощник (на каждый начатый кейс, за год)
    fin_enabled: bool = True
    fin_deposit_share: float = 0.35   # доля клиентов, открывших вклад или накопительный счёт (новые деньги)
    fin_avg_deposit: int = 1_200_000  # средний остаток
    fin_deposit_margin: float = 0.02  # маржа банка на привлечённых средствах, годовых
    fin_invest_share: float = 0.10    # доля клиентов, открывших брокерский счёт
    fin_avg_invest: int = 400_000
    fin_invest_fee: float = 0.012     # комиссии брокера и управляющей компании, годовых


def unit(i: Inputs) -> dict:
    loan = i.price * i.ltv
    savings = i.price * i.savings_pct
    lines = [
        {"name": "Плата за результат", "note": f"экономия {fmt_pct(i.savings_pct)} × {fmt_pct(i.fee_pct)}", "value": savings * i.fee_pct},
        {"name": "Маржа по ипотеке", "note": f"{fmt_pct(i.mortgage_margin_pct)} от кредита × {fmt_pct(i.incrementality)} инкрементальности", "value": loan * i.mortgage_margin_pct * i.incrementality},
        {"name": "Страхование", "note": "доп. комиссия на сделку", "value": i.insurance},
        {"name": "Сервисы сделки", "note": "СБР, электронная регистрация, проверка", "value": i.services},
        {"name": "Экономия времени менеджеров", "note": f"{i.manager_hours:g} ч × {i.manager_rate} ₽", "value": i.manager_hours * i.manager_rate},
    ]
    revenue = sum(x["value"] for x in lines)
    cost_per_deal = i.case_cost / i.conversion
    variable = cost_per_deal + i.reserve
    contribution = revenue - variable

    cases = i.deals / i.conversion if i.conversion else 0
    fin_per_case = (i.fin_deposit_share * i.fin_avg_deposit * i.fin_deposit_margin
                    + i.fin_invest_share * i.fin_avg_invest * i.fin_invest_fee) if i.fin_enabled else 0
    fin_year = cases * fin_per_case

    profit_core = i.deals * contribution - i.fixed_costs
    profit = profit_core + fin_year
    margin_loss = loan * i.savings_pct * i.mortgage_margin_pct
    per_deal_total = contribution + (fin_per_case / i.conversion if i.conversion else 0)
    return {
        "inputs": asdict(i),
        "lines": [{**x, "value": round(x["value"])} for x in lines],
        "revenue": round(revenue),
        "cost_per_deal": round(cost_per_deal),
        "variable": round(variable),
        "contribution": round(contribution),
        "profit": round(profit),
        "roi": profit / i.fixed_costs if i.fixed_costs else None,
        "roi_core": profit_core / i.fixed_costs if i.fixed_costs else None,
        "breakeven_deals": round(i.fixed_costs / per_deal_total) if per_deal_total > 0 else None,
        "breakeven_core": round(i.fixed_costs / contribution) if contribution > 0 else None,
        "fin": {
            "enabled": i.fin_enabled,
            "cases": round(cases),
            "per_case": round(fin_per_case),
            "per_deal": round(fin_per_case / i.conversion) if i.conversion else 0,
            "year": round(fin_year),
        },
        "client": {"savings": round(savings), "fee": round(savings * i.fee_pct), "net": round(savings * (1 - i.fee_pct))},
        "conflict": {
            "loan_reduction": round(loan * i.savings_pct),
            "margin_loss": round(margin_loss),
            "ratio": round(contribution / margin_loss, 1) if margin_loss else None,
        },
    }


SCENARIOS = {
    "pilot": ("Пилот, 1-й год", dict(deals=2_000, fixed_costs=150_000_000)),
    "pessimistic": ("Пессимистичный, 2-й год", dict(savings_pct=0.015, incrementality=0.15, conversion=0.15, deals=6_000,
                                                    fixed_costs=200_000_000, fin_deposit_share=0.2, fin_deposit_margin=0.015)),
    "base": ("Базовый, 2-й год", dict(deals=15_000, fixed_costs=200_000_000)),
    "optimistic": ("Оптимистичный, 2-й год", dict(savings_pct=0.04, incrementality=0.40, conversion=0.35, deals=25_000,
                                                  fixed_costs=250_000_000, fin_deposit_share=0.45, fin_deposit_margin=0.025,
                                                  fin_invest_share=0.15)),
}


def scenarios(base: Inputs | None = None) -> list[dict]:
    base = base or Inputs()
    out = []
    for key, (title, params) in SCENARIOS.items():
        r = unit(replace(base, **params))
        out.append({"key": key, "title": title, **r})
    return out


def fmt_pct(v: float) -> str:
    return f"{v * 100:g}%"
