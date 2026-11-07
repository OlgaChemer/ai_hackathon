"""Проверка используемого подмножества JSON Schema до изменения состояния.

Поддерживаются только типы, enum, properties/items, required и числовые границы,
которые объявляют наши действия. Размеры ограничены независимо от схемы модели.
"""
import math


def validate(value, schema: dict, depth: int = 0) -> None:
    kind = schema.get("type")
    if isinstance(kind, list):
        if value is None and "null" in kind:
            return
        kind = next(t for t in kind if t != "null")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": type(value) is int, "number": type(value) in (int, float)}
    # Native places.words имеет пять уровней; глубина всё равно ограничена.
    if depth > 6 or not valid.get(kind, False):
        raise ValueError("Неверный тип или глубина параметров")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Значение отсутствует среди допустимых вариантов")
    if kind in ("integer", "number"):
        if (type(value) is float and not math.isfinite(value)) or not schema.get("minimum", -1e12) <= value <= schema.get("maximum", 1e12):
            raise ValueError("Число вне допустимого диапазона")
    elif kind == "string" and not schema.get('minLength',0) <= len(value) <= schema.get("maxLength", 2000):
        raise ValueError("Неверная длина текста")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError("Слишком мало элементов")
        if len(value) > schema.get("maxItems", 100):
            raise ValueError("Слишком много элементов")
        for index,item in enumerate(value):
            try:validate(item, schema["items"], depth + 1)
            except ValueError as exc:raise ValueError(f'[{index}]: {exc}') from exc
    elif kind == "object":
        props = schema.get("properties", {})
        if len(value) > 64 or value.keys() - props.keys() or set(schema.get("required", [])) - value.keys():
            raise ValueError("Неизвестные или отсутствующие параметры")
        for key, item in value.items():
            try:validate(item, props[key], depth + 1)
            except ValueError as exc:raise ValueError(f'{key}: {exc}') from exc


TEXT = {"type": "string", "maxLength": 1000}
IDS = {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 80}}


def number(lo=0, hi=1_000_000_000):
    return {"type": "number", "minimum": lo, "maximum": hi}


def choice(*values):
    return {"type": "string", "enum": list(values)}


CONFIRMATIONS = {
    "clarify": {}, "price": {}, "object": {}, "sign": {},
    "red_lines": {"budget_max": number(100_000)},
    "call": {"mode": choice("auto", "human", "phone"),
             'phone_grant_id':{'type':'string','maxLength':40},
             "questions":{"type":"array","maxItems":15,"items":{"type":"string","maxLength":300}}},
    "payment": {"payment": choice("cash", "mortgage_ready", "mortgage_saving")},
    "housing": {"housing": choice("secondary", "new", "both")},
    "family": {"children": number(0, 30), "youngest_age": {"type": ["number", "null"], "minimum": 0, "maximum": 100}, "matcap": number()},
    "finance": {"monthly_income": number(), "monthly_expenses": number(), "savings": number(),
                "horizon_months": number(1, 600), "risk": choice("conservative", "moderate", "aggressive")},
    "selection": {"action": choice("proceed", "more", "liked", "text"), "liked": IDS, "disliked": IDS},
}


def confirmation(kind: str, data: dict) -> None:
    if kind not in CONFIRMATIONS:
        raise ValueError("Неизвестная карточка")
    validate(data, {"type": "object", "properties": {
        "text": TEXT, "_said": TEXT, "_label": TEXT, **CONFIRMATIONS[kind]}})
