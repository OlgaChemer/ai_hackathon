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
from ..workflow import available, programs_current

log = logging.getLogger(__name__)


class ActionError(Exception):
    """Действие нельзя выполнить сейчас — текст уходит модели как результат."""


@dataclass(frozen=True)
class Action:
    name: str
    step: int | None            # этап диалога, который действие делает активным
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


async def execute(s, call: ToolCall) -> dict:
    """Выполнить действие, которое выбрала модель. Ошибки не роняют агента — модель получает объяснение."""
    _load()
    a = REGISTRY.get(call.name) if isinstance(call.name, str) else None
    if a is None:
        return {"error": f"Нет действия с таким именем. Доступны: {', '.join(REGISTRY)}"}
    # Семантический разбор выполняется до проверки допуска: новые условия могут отозвать согласия.
    if s.request_inputs:
        from .. import understanding
        # Запрос извлекается из принятого ввода, независимо от аргументов планировщика.
        checked = await understanding.ensure(s)
        if checked and checked.get("needs_clarification"):
            return checked
        if call.name == 'update_request':
            if a.step:
                await s.step(a.step)
            if checked and checked.get('profile_result'):
                return checked['profile_result']
            return await a.run(s)  # уже проверенный ввод; повторная догадка не применяется
    replay = (call.name == "confirm_red_lines" and s.red_lines_ok) or (
        call.name == "mortgage_programs" and programs_current(s))
    if s.requested_action and available(s,s.requested_action) and call.name not in ('update_request',s.requested_action):
        return {'error':'Сначала выполните явно запрошенную карточку: '+s.requested_action}
    if not replay and not available(s, call.name):
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
    if a.step and not replay:
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
