"""Версионированные допущения расчётов; загрузка не обращается к сети или секретам."""

import hashlib
import json
import math
from pathlib import Path


def validate(policy):
    """Ошибка конфигурации останавливает запуск, а не подменяется скрытым тарифом."""
    if (
        not policy.get("version")
        or policy.get("mode") != "demo"
        or not policy.get("note")
    ):
        raise ValueError("Нужны версия, режим demo и описание допущений")

    def number(value, lo=0, hi=1):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not lo <= value <= hi
        ):
            raise ValueError(f"Число политики вне диапазона {lo}…{hi}")

    m = policy["mortgage"]
    for key in ("market_base", "family_min_down", "post_subsidy_rate"):
        number(m[key], 0.001, 0.99)
    number(m["years"], 1, 100)
    number(m["subsidy_years"], 1, m["years"] - 1)
    number(m["family_child_age"], 1, 30)
    number(m["matcap_first"], 0, 10_000_000)
    for threshold, surcharge in m["market_surcharges"]:
        number(threshold)
        number(surcharge)
    number(m["default_down_share"], 0.001, 0.99)
    for share, years in m["scenarios"]:
        number(share, 0.001, 0.99)
        number(years, 1, 100)
    for key, value in m["costs"].items():
        number(value, 0, 1 if key.endswith("_rate") else 1_000_000)
    for scale in m["family_scale"].values():
        if set(scale) != {"1", "2", "3", "4", "5"}:
            raise ValueError("Нужны условия для каждого числа детей 1…5")
        for rate, limit in scale.values():
            number(rate, 0.001, 0.99)
            number(limit, 1, 100_000_000)
    products = policy["savings"]["products"]
    for product in products.values():
        for key in ("rate", "low", "high"):
            number(product[key], -0.99, 1)
        if not product["low"] <= product["rate"] <= product["high"]:
            raise ValueError("Ожидаемая доходность должна быть внутри диапазона")
    for profile in policy["savings"]["profiles"].values():
        if not set(profile["mix"]) <= products.keys() or not math.isclose(
            sum(profile["mix"].values()), 1
        ):
            raise ValueError(
                "Состав стратегии должен состоять из продуктов и давать 100%"
            )
        for weight in profile["mix"].values():
            number(weight)
    savings = policy["savings"]
    for key in ("contribution_share", "payment_income_share"):
        number(savings[key], 0.001, 1)
    for key in ("search_months", "aggressive_min_months", "cushion_months"):
        number(savings[key], 1, 120)
    for value in savings["bank_margins"].values():
        number(value)
    for key, value in policy["economics"].items():
        if key != "fin_enabled":
            number(value, 0, 1e12)
    number(policy["economics"]["fee_pct"])
    number(policy["economics"]["conversion"], 0.001, 1)
    pricing = policy["pricing"]
    number(pricing["bargain_discount"], 0.01, 1)
    for value in pricing["condition"].values():
        number(value, 0.1, 3)
    for key in ("first_floor", "last_floor"):
        number(pricing[key], 0.1, 3)
    for value in pricing["rank"].values():
        number(value, 0.001, 1000)
    if pricing["rank"]["relative_min"] > pricing["rank"]["relative_max"]:
        raise ValueError("Неверный диапазон поправки ранжирования")
    for year, factor in pricing["year"]:
        number(year, 0, 2100)
        number(factor, 0.1, 3)
    comparables = pricing["comparables"]
    for key, value in comparables.items():
        number(value, 0.001, 1000)
    for key in ("nearby_limit", "city_limit", "minimum"):
        if type(comparables[key]) is not int or comparables[key] < 4:
            raise ValueError("Для оценки нужны минимум четыре аналога")
    number(comparables["min_band"], 0.001, 0.99)
    if comparables["area_min_ratio"] > comparables["area_max_ratio"]:
        raise ValueError("Неверный диапазон площади аналогов")


def validate_products(policy):
    """Предложение Сбера держится у рынка: скидки малы, тарифы и сроки правдоподобны."""
    p = policy["products"]
    if not p.get("as_of") or not p.get("sources"):
        raise ValueError("Продукты: нужны дата и источники цифр")
    for key in ("life_insurance_rate", "property_insurance_rate", "title_insurance_rate"):
        if not 0 < p[key] <= 0.02:
            raise ValueError(f"Продукты: тариф {key} вне 0…2%")
    for key in ("life_refusal_surcharge", "salary_discount"):
        if not 0 <= p[key] <= 0.03:
            raise ValueError(f"Продукты: {key} вне 0…3 п.п.")
    account = p["welcome_account"]
    if not (0 < account["rate"] < 0.5 and 1 <= account["days"] <= 366 and account["name"]):
        raise ValueError("Продукты: неверный накопительный счёт")
    if not 0 < p["market_deposit_rate"] < 0.5:
        raise ValueError("Продукты: неверная рыночная ставка вкладов")
    if not 0 < p["min_down_share"] < 1 or not 0 <= p["min_cushion_months"] <= policy["savings"]["cushion_months"]:
        raise ValueError("Продукты: неверный минимальный взнос или подушка")
    if set(p["days_to_settlement"]) != {"cash", "mortgage", "new"} or not all(
            1 <= v <= 90 for v in p["days_to_settlement"].values()):
        raise ValueError("Продукты: неверные сроки до расчётов")


_raw = Path(__file__).with_suffix(".json").read_bytes()
POLICY = json.loads(_raw)
validate(POLICY)
validate_products(POLICY)
FINGERPRINT = hashlib.sha256(_raw).hexdigest()[:12]


def metadata():
    return {key: POLICY[key] for key in ("version", "mode", "note")} | {
        "fingerprint": FINGERPRINT
    }
