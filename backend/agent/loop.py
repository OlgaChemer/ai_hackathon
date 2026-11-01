"""Цикл агента: модель видит разговор, состояние сделки и действия — и сама решает, что делать.

  модель вызвала действия -> код выполняет (карточки ждут клиента) -> результаты модели -> снова решает
  модель написала текст    -> сообщение клиенту -> ждём, что он ответит
Без модели агент не работает: если она не ответила, агент так и говорит и продолжит, когда клиент напишет.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid

from .. import geo
from ..llm import LLMError
from . import actions, guard, prompts
from .features import FEATURES
from .numbers import fmt
from .profile import missing_required

log = logging.getLogger(__name__)

MAX_ACTIONS_IN_ROW = 14  # действий подряд без ответа клиенту — дальше модель должна заговорить с клиентом


def state_brief(s) -> str:
    """Что уже известно и сделано — модель видит это каждый ход."""
    p = s.profile
    lines = []
    if p:
        known = {k: v for k, v in p.items() if k not in ("summary", "market") and v not in (None, [], "")}
        if known.get("features"):
            known["features"] = [FEATURES[k][0] for k in known["features"]]
        if known.get("poi"):
            known["poi"] = [geo.POI[k][0] for k in known["poi"]]
        lines.append("Запрос: " + json.dumps(known, ensure_ascii=False))
        missing = missing_required(p)
        lines.append("Не хватает для поиска: " + (", ".join(missing) if missing else "ничего"))
    else:
        lines.append("Запрос ещё не записан.")
    lines.append("Рамки клиентом " + ("подтверждены." if s.red_lines_ok else "НЕ подтверждены."))
    if s.mortgage:
        chosen = next(c for c in s.mortgage["programs"] if c["key"] == s.mortgage["chosen"])
        lines.append(f"Ипотека посчитана: {chosen['title']}, {chosen['rate'] * 100:.1f}%, платёж {fmt(chosen['monthly'])}.")
    if s.finance:
        lines.append(f"План накоплений: не хватает {fmt(s.finance['gap'])}, срок {s.finance['months']} мес.")
    if s.search:
        lines.append(f"Поиск: найдено {len(s.search['listings'])}, в бюджете {len(s.search['pool'])}.")
    if s.shown:
        cards = "; ".join(f"{i + 1}) {x['id']} — {x.get('address')}, {fmt(x['price'])}" for i, x in enumerate(s.shown))
        lines.append(f"Последняя подборка: {cards}. Нравятся: {', '.join(s.prefs.liked) or 'ничего'}.")
    for lid, c in s.calls.items():
        lines.append(f"Звонок {lid}: {'договорились' if c['status'] == 'agreed' else 'не договорились'} на {fmt(c['price'])}"
                     + (", цену подтвердил клиент" if lid in s.agreed else "") + (", квартира выбрана" if s.chosen == lid else ""))
    return "\n".join(lines)


async def think(s, with_actions: bool):
    messages = [{"role": "system", "content": prompts.AGENT_SYSTEM + "\n" + guard.POLICY},
                guard.data_message("session_state", state_brief(s)), *guard.output(s.messages, limit=False)]
    return await s.llm.chat(messages, tools=actions.tools(s) if with_actions else None, temperature=0.3, max_tokens=900,
                            tag="agent")


async def run(s) -> None:
    """Агент работает, пока открыт диалог: отвечает, действует, ждёт клиента. Остановить — отменить задачу."""
    s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    if s.query:
        s.push("message", role="user", text=s.query)
        if s.refuse(s.query):
            s.messages.append({"role": "user", "content": await s.inbox.get()})
        else:
            s.messages.append({"role": "user", "content": s.query})
    in_row = 0
    while True:
        s.signal("typing", on=True)
        try:
            resp = await think(s, with_actions=in_row < MAX_ACTIONS_IN_ROW)
        except LLMError as exc:
            log.warning("модель не ответила: %s", type(exc).__name__)
            s.signal("typing", on=False)
            s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=True, reason="Модель временно недоступна")
            await s.say(f"Модель {s.llm.name} сейчас не отвечает — продолжить не могу. Напишите, когда попробовать снова: "
                        "продолжу с того же места.")
            s.remember()
            s.messages.append({"role": "user", "content": await s.inbox.get()})
            continue
        if resp.tool_calls:
            if len(resp.tool_calls) > MAX_ACTIONS_IN_ROW - in_row:
                await s.say("Достигнут лимит действий за один ход. Напишите, чтобы продолжить.")
                s.signal("typing", on=False)
                s.messages.append({"role": "user", "content": await s.inbox.get()})
                in_row = 0
                continue
            in_row += len(resp.tool_calls)
            calls = [c if c.id else dataclasses.replace(c, id=f"call_{uuid.uuid4().hex[:8]}") for c in resp.tool_calls]
            s.messages.append({"role": "assistant", "content": guard.output(resp.text or "", s), "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(guard.output(guard.sanitize(c.arguments), limit=False), ensure_ascii=False)}}
                for c in calls]})
            if resp.functions_state_id:
                s.messages[-1]["functions_state_id"] = resp.functions_state_id
            s.remember()  # до действий: если сервер упадёт, пока карточка ждёт клиента, вызов не потеряется
            if resp.text.strip():
                await s.say(resp.text.strip())
            s.signal("typing", on=False)
            for call in calls:
                log.info("агент %s: вызов действия", s.id)
                revision = s.client_revision
                result = await actions.execute(s, call)
                if s.client_revision != revision:  # карточку подтвердил человек: начался новый ход клиента
                    in_row = 0
                s.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(guard.output(guard.sanitize(result), limit=False), ensure_ascii=False, default=str)})
            s.remember()
            continue
        in_row = 0
        s.signal("typing", on=False)
        text = guard.output(resp.text.strip(), s)
        if text:
            await s.say(text, await_reply=True)
            s.messages.append({"role": "assistant", "content": text, **({"functions_state_id": resp.functions_state_id} if resp.functions_state_id else {})})
        s.remember()
        s.messages.append({"role": "user", "content": await s.inbox.get()})


async def run_session(s) -> None:
    try:
        await run(s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # показать ошибку в интерфейсе, а не молча упасть
        log.error("агент упал (%s)", type(exc).__name__)
        s.push("error", text="Ошибка агента. Попробуйте продолжить диалог.")
