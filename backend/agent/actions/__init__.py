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
    from .money import programs_current
    p = s.profile
    if s.request_intent == "hold" and name not in ("update_request", "ask_client"):
        return False
    work_intent = s.request_intent in (None, "search", "refine", "continue")
    ready = bool(p.get("city") and p.get("rooms") is not None and p.get("budget_max"))
    finance_ready = p.get("payment") == "cash" or (s.mortgage is not None and
                    (p.get("payment") != "mortgage_saving" or s.finance is not None))
    rules = {
        "update_request": not s.request_inputs or any(x['seq'] > s.request_review_seq for x in s.request_inputs),
        # Подтверждение рамок завершает вводную анкету. Если поиск ничего не даст,
        # снова разрешаем обсудить изменение условий.
        "ask_client": not ready or not s.red_lines_ok or s.search is not None,
        "confirm_red_lines": ready and not s.red_lines_ok,
        "market_prices": bool(p.get("city") and p.get("rooms") is not None),
        "ask_payment": not p.get("payment"),
        "ask_housing": not p.get("housing"),
        "ask_family": p.get("children") is None and p.get("payment") != "cash",
        "mortgage_programs": not programs_current(s) and ready and bool(p.get("payment")) and
                             (p.get("payment") == "cash" or p.get("children") is not None),
        "savings_plan": ready and p.get("payment") == "mortgage_saving",
        "search_listings": ready and s.red_lines_ok and bool(p.get("payment")) and work_intent,
        "show_selection": s.search is not None,
        "offer_search_alternatives": s.search is not None,
        "probe_budget": s.search is not None,
        "call_seller": s.search is not None and s.red_lines_ok and bool(p.get("payment")) and work_intent,
        "confirm_price": bool(s.calls) and s.red_lines_ok,
        "plan_viewing": bool(s.agreed) and s.red_lines_ok,
        "prepare_deal": bool(s.chosen) and s.red_lines_ok and finance_ready and s.status.get(8) != "done",
    }
    return bool(rules.get(name, True))


async def execute(s, call: ToolCall) -> dict:
    """Выполнить действие, которое выбрала модель. Ошибки не роняют агента — модель получает объяснение."""
    _load()
    a = REGISTRY.get(call.name) if isinstance(call.name, str) else None
    if a is None:
        return {"error": f"Нет действия с таким именем. Доступны: {', '.join(REGISTRY)}"}
    # Семантический разбор выполняется до проверки допуска: новые условия могут отозвать согласия.
    if s.request_inputs:
        from .. import understanding
        # Предложение планировщика — только данные. Неверный тип его аргумента
        # не должен блокировать независимое понимание корректного сообщения клиента.
        proposal = call.arguments if call.name == 'update_request' and isinstance(call.arguments, dict) else None
        checked = await understanding.ensure(s, proposal)
        if checked and checked.get("needs_clarification"):
            return checked
        if call.name == 'update_request':
            if a.step:
                await s.step(a.step)
            if checked and checked.get('profile_result'):
                return checked['profile_result']
            return await a.run(s)  # уже проверенный ввод; повторная догадка не применяется
    if call.name == "confirm_red_lines" and s.red_lines_ok:
        return await a.run(s)
    if call.name == "mortgage_programs":
        from .money import programs_current
        if programs_current(s):
            return await a.run(s)
    if not available(s, call.name):
        names = ", ".join(a.name for a in REGISTRY.values() if available(s, a.name))
        hint = ""
        if call.name == "search_listings":
            hint = "Для поиска нужны город, комнаты и бюджет (update_request), confirm_red_lines и способ покупки (ask_payment). "
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
