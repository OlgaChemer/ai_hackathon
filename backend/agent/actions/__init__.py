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


def tools() -> list[Tool]:
    _load()
    return [Tool(a.name, a.description, a.parameters) for a in REGISTRY.values()]


async def execute(s, call: ToolCall) -> dict:
    """Выполнить действие, которое выбрала модель. Ошибки не роняют агента — модель получает объяснение."""
    _load()
    a = REGISTRY.get(call.name) if isinstance(call.name, str) else None
    if a is None:
        return {"error": f"Нет действия с таким именем. Доступны: {', '.join(REGISTRY)}"}
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
