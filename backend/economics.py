"""Юнит-экономика сделки и ROI для Сбера (формулы из бизнес-модели, все числа — входы)."""
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
    profit = i.deals * contribution - i.fixed_costs
    margin_loss = loan * i.savings_pct * i.mortgage_margin_pct
    return {
        "inputs": asdict(i),
        "lines": [{**x, "value": round(x["value"])} for x in lines],
        "revenue": round(revenue),
        "cost_per_deal": round(cost_per_deal),
        "variable": round(variable),
        "contribution": round(contribution),
        "profit": round(profit),
        "roi": profit / i.fixed_costs if i.fixed_costs else None,
        "breakeven_deals": round(i.fixed_costs / contribution) if contribution > 0 else None,
        "client": {
            "savings": round(savings),
            "fee": round(savings * i.fee_pct),
            "net": round(savings * (1 - i.fee_pct)),
        },
        "conflict": {
            "loan_reduction": round(loan * i.savings_pct),
            "margin_loss": round(margin_loss),
            "ratio": round(contribution / margin_loss, 1) if margin_loss else None,
        },
    }


SCENARIOS = {
    "pilot": ("Пилот, 1-й год", dict(deals=2_000, fixed_costs=150_000_000)),
    "pessimistic": ("Пессимистичный, 2-й год", dict(savings_pct=0.015, incrementality=0.15, conversion=0.15, deals=6_000, fixed_costs=200_000_000)),
    "base": ("Базовый, 2-й год", dict(deals=15_000, fixed_costs=200_000_000)),
    "optimistic": ("Оптимистичный, 2-й год", dict(savings_pct=0.04, incrementality=0.40, conversion=0.35, deals=25_000, fixed_costs=250_000_000)),
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
