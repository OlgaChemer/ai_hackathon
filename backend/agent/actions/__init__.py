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
from ..capabilities import decision, POLICIES, catalog, argument_denial

log = logging.getLogger(__name__)


class ActionError(Exception):
    """Действие нельзя выполнить сейчас — текст уходит модели как результат."""
    def __init__(self,message,kind='action_unavailable'):
        super().__init__(message);self.kind=kind


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
        required=[key for key,param in inspect.signature(fn).parameters.items()
                  if key!='s' and param.default is inspect.Parameter.empty]
        policy=POLICIES.get(name)
        schema_properties=properties
        if policy:
            schema_properties={k:{**v,**({'description':v.get('description','')+' Единица: '+dict(policy.units)[k]+'.'}
                         if k in dict(policy.units) else {})} for k,v in properties.items()}
        schema={"type":"object","properties":schema_properties,"additionalProperties":False,"required":required}
        REGISTRY[name] = Action(name, step, description, schema, fn)
        return fn
    return wrap


def tools(s=None) -> list[Tool]:
    _load()
    return [Tool(a.name, a.description, a.parameters) for a in REGISTRY.values()
            if a.name in POLICIES and (s is None or available(s, a.name))]


async def execute(s, call: ToolCall) -> dict:
    from ..operations import run
    return await run(s,call,_execute)


async def _execute(s, call: ToolCall) -> dict:
    """Выполнить действие, которое выбрала модель. Ошибки не роняют агента — модель получает объяснение."""
    _load()
    a = REGISTRY.get(call.name) if isinstance(call.name, str) else None
    if a is None:
        return {"error_kind":"unsupported_action","error": f"Нет действия с таким именем. Доступны: {', '.join(REGISTRY)}"}
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
        hint = (decision(s,call.name)[1] or "")+" "
        return {"error_kind":"action_unavailable","error": f"{hint}Действие недоступно в текущем состоянии. Доступны: {names}"}
    args = call.arguments
    try:
        validation.validate(args, a.parameters)
        inspect.signature(a.run).bind(s, **args)
    except (ValueError, TypeError):
        return {"error_kind":"model_arguments","error": "Неверные параметры действия: проверь типы, диапазоны и обязательные поля."}
    denial=argument_denial(s,call.name,args)
    if denial:return {'error_kind':'action_unavailable','error':denial}
    verdict = guard.input_verdict(args)
    if not verdict.ok or guard.output(args, limit=False) != args:
        guard.note(s, "tool", verdict.category or "unsafe_output", "")
        return {"error_kind":"invalid_arguments","error": "Параметры действия отклонены проверкой безопасности."}
    worker=s.fork()
    if a.step and not replay:
        await worker.step(a.step)
    try:
        result=await a.run(worker, **args)
    except ActionError as exc:
        result={"error_kind":exc.kind,"error":str(exc)}
    worker.flush(result=result)
    if s.requested_action==call.name:
        s.requested_action=None
        s.profile.pop('_requested_action',None)
        s.remember()
    return result


def _load() -> None:
    """Модули с действиями регистрируются при импорте."""
    from . import deal, money, request, search  # noqa: F401
