"""Действия агента: что модель может сделать. Каждое — обычная функция кода с описанием для модели.

Модель выбирает действие и параметры, код выполняет и возвращает результат (короткий JSON) — модель решает дальше.
Числа считает код, рамки проверяет код: действие, которое нарушает порядок (поиск без подтверждённых рамок,
подписание без выбранной квартиры), не выполняется и возвращает модели объяснение, что сделать сначала.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ...llm import Tool, ToolCall
from .. import guard, validation

log = logging.getLogger(__name__)


class ActionError(Exception):
    """Действие нельзя выполнить сейчас — текст уходит модели как результат."""


@dataclass(frozen=True)
class Action:
    name: str
    step: int | None            # шаг степпера, который действие делает активным
    description: str
    parameters: dict[str, Any]  # JSON Schema параметров
    run: Callable[..., Awaitable[dict]]


REGISTRY: dict[str, Action] = {}


def action(name: str, step: int | None, description: str, **properties: dict) -> Callable:
    """Регистрирует действие: @action("search_listings", 3, "что делает", more_pages={"type": "integer", ...})."""
    def wrap(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        REGISTRY[name] = Action(name, step, description,
                                {"type": "object", "properties": properties, "additionalProperties": False}, fn)
        return fn
    return wrap


def tools(s=None) -> list[Tool]:
    _load()
    return [Tool(a.name, a.description, a.parameters) for a in REGISTRY.values() if s is None or available(s, a.name)]


def available(s, name: str) -> bool:
    """Модель выбирает действия среди разрешённых текущим состоянием, порядок не диктуется."""
    p = s.profile
    ready = bool(p.get("city") and p.get("rooms") is not None and p.get("budget_max"))
    finance = p.get("payment") == "cash" or (s.mortgage is not None and
               (p.get("payment") != "mortgage_saving" or s.finance is not None))
    rules = {
        # Подтверждение рамок завершает вводную анкету. Если поиск ничего не даст,
        # снова разрешаем обсудить изменение условий.
        "ask_client": not ready or not s.red_lines_ok or s.search is not None,
        "confirm_red_lines": ready and not s.red_lines_ok,
        "market_prices": bool(p.get("city") and p.get("rooms") is not None),
        "ask_payment": not p.get("payment"),
        "ask_housing": not p.get("housing"),
        "ask_family": p.get("children") is None and p.get("payment") != "cash",
        "mortgage_programs": ready and bool(p.get("payment")) and
                             (p.get("payment") == "cash" or p.get("children") is not None),
        "savings_plan": ready and p.get("payment") == "mortgage_saving",
        "search_listings": ready and s.red_lines_ok and finance,
        "show_selection": s.search is not None and s.red_lines_ok,
        "probe_budget": s.search is not None,
        "call_seller": s.search is not None and s.red_lines_ok,
        "confirm_price": bool(s.calls) and s.red_lines_ok,
        "plan_viewing": bool(s.agreed) and s.red_lines_ok,
        "prepare_deal": bool(s.chosen) and s.red_lines_ok and s.status.get(8) != "done",
    }
    return bool(rules.get(name, True))


async def execute(s, call: ToolCall) -> dict:
    """Выполнить действие, которое выбрала модель. Ошибки не роняют агента — модель получает объяснение."""
    _load()
    a = REGISTRY.get(call.name) if isinstance(call.name, str) else None
    if a is None:
        return {"error": f"Нет действия с таким именем. Доступны: {', '.join(REGISTRY)}"}
    if not available(s, call.name):
        names = ", ".join(a.name for a in REGISTRY.values() if available(s, a.name))
        hint = ""
        if call.name == "search_listings":
            hint = "Для поиска нужны confirm_red_lines, ask_payment и, при ипотеке, ask_family, mortgage_programs (savings_plan, если взнос ещё копят). "
        return {"error": f"{hint}Действие недоступно в текущем состоянии. Доступны: {names}"}
    args = call.arguments
    try:
        validation.validate(args, a.parameters)
        inspect.signature(a.run).bind(s, **args)
    except (ValueError, TypeError):
        return {"error": "Неверные параметры действия: проверь типы, диапазоны и обязательные поля."}
    verdict = guard.input_verdict(args)
    if not verdict.ok or guard.output(args, limit=False) != args:
        guard.note(s, "tool", verdict.category or "unsafe_output", "")
        return {"error": "Параметры действия отклонены проверкой безопасности."}
    if a.step:
        await s.step(a.step)
    try:
        return await a.run(s, **args)
    except ActionError as exc:
        return {"error": str(exc)}
    except TypeError:  # модель передала параметры не того вида
        log.warning("неверные параметры действия %s", call.name)
        return {"error": "Неверные параметры действия."}


def _load() -> None:
    """Модули с действиями регистрируются при импорте."""
    from . import deal, money, request, search  # noqa: F401
