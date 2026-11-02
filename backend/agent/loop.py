"""Цикл агента: модель видит разговор, состояние сделки и действия — и сама решает, что делать.

  модель вызвала действия -> код выполняет (карточки ждут клиента) -> результаты модели -> снова решает
  модель написала текст    -> сообщение клиенту -> ждём, что он ответит
Без модели агент не работает: если она не ответила, агент так и говорит и продолжит, когда клиент напишет.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid

from .. import geo
from ..llm import LLMError, LLMResponse, ToolCall
from ..llm.base import LLMBusy
from ..config import settings
from ..runtime import Turn, TurnStopped, record
from . import actions, guard, prompts, understanding
from .features import FEATURES
from .numbers import fmt
from .profile import missing_required
from .session import close_dangling_calls

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
    if s.request_intent:
        lines.append("Проверенное намерение клиента: " + s.request_intent + ".")
    return "\n".join(lines)


async def think(s, with_actions: bool):
    messages = [{"role": "system", "content": prompts.AGENT_SYSTEM + "\n" + guard.POLICY},
                guard.data_message("session_state", state_brief(s)), *guard.output(s.messages, limit=False)]
    return await s.llm.chat(messages, tools=actions.tools(s) if with_actions else None, temperature=0.3, max_tokens=900,
                            tag="agent")



def pending_search_action(s):
    """Read-only progress has no financial authority; never auto-call or auto-confirm."""
    if not missing_required(s.profile):
        if s.search is None:
            return "search_listings"
        if not s.shown:
            from .actions.search import selectable
            return "show_selection" if any(selectable(x, s.prefs) for x in s.search["pool"]) else "offer_search_alternatives"
    return None


async def run(s) -> None:
    """Агент работает, пока открыт диалог: отвечает, действует, ждёт клиента. Остановить — отменить задачу."""
    s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    if s.query:
        initial = s.push("message", role="user", text=s.query)
        if s.refuse(s.query):
            s.messages.append({"role": "user", "content": await s.inbox.get()})
        else:
            s.accept_request(initial["text"], initial["seq"])
            s.messages.append({"role": "user", "content": s.query})
    turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
    forced = None
    auto_progress = set()
    while True:
        s.signal("typing", on=True)
        interrupted_call = None
        try:
            # Ответ в открытой карточке уже проверен HTTP-обработчиком: применяем к границе хода.
            if s.request_review and s.request_review['seq'] > s.request_review_seq:
                checked = await turn.run(lambda: understanding.ensure(s), kind="tool", name="review_input")
                if checked and checked.get("needs_clarification"):
                    s.messages.extend([{"role":"assistant","content":checked['question']},
                                       {"role":"user","content":checked['client_said']}])
                    s.remember()
                    continue
            if forced:
                resp = LLMResponse("", [ToolCall(forced, {}, f"progress_{uuid.uuid4().hex[:8]}")])
                record("search_recovery", session=s.id, action=forced)
                forced = None
            else:
                resp = await turn.run(lambda: think(s, with_actions=turn.tool_calls < MAX_ACTIONS_IN_ROW),
                                      kind="llm", name="agent")
            if not resp.tool_calls and any(x['seq'] > s.request_review_seq for x in s.request_inputs):
                checked = await turn.run(lambda: understanding.ensure(s), kind="tool", name="review_input")
                if checked and checked.get("needs_clarification"):
                    s.messages.extend([{"role":"assistant","content":checked['question']},
                                       {"role":"user","content":checked['client_said']}])
                    s.remember()
                    continue
            next_action = pending_search_action(s)
            if not resp.tool_calls and next_action and s.request_intent in ("search", "refine", "continue") and next_action not in auto_progress and turn.tool_calls < MAX_ACTIONS_IN_ROW:
                s.messages.append({"role": "assistant", "content": guard.output(resp.text or "", s),
                    **({"functions_state_id": resp.functions_state_id} if resp.functions_state_id else {})})
                auto_progress.add(next_action)
                forced = next_action
                continue
            if resp.tool_calls:
                if len(resp.tool_calls) > MAX_ACTIONS_IN_ROW - turn.tool_calls:
                    raise TurnStopped("tool_limit")
                calls = [c if c.id else dataclasses.replace(c, id=f"call_{uuid.uuid4().hex[:8]}") for c in resp.tool_calls]
                s.messages.append({"role": "assistant", "content": guard.output(resp.text or "", s), "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(guard.output(guard.sanitize(c.arguments), limit=False), ensure_ascii=False)}}
                    for c in calls]})
                if resp.functions_state_id:
                    s.messages[-1]["functions_state_id"] = resp.functions_state_id
                s.remember()  # вызов не потеряется, если сервер остановится на карточке
                if resp.text.strip():
                    await s.say(resp.text.strip())
                s.signal("typing", on=False)
                searched = False
                for call in calls:
                    interrupted_call = call.name
                    turn.check_repeat(call.name, call.arguments)
                    revision = s.client_revision
                    # В логе только известное имя действия, без аргументов модели.
                    name = call.name if call.name in actions.REGISTRY else "unknown"
                    result = await turn.run(lambda: actions.execute(s, call), kind="tool", name=name)
                    if call.name == "search_listings" and not result.get("error"):
                        searched = True
                    if call.name == "update_request" and result.get("already_reviewed") and s.request_intent in ("search", "refine", "continue"):
                        searched = True
                    s.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(guard.output(guard.sanitize(result), limit=False), ensure_ascii=False, default=str)})
                    if s.client_revision != revision:
                        turn.finish("client_answer")
                        auto_progress.clear()
                        turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
                s.remember()
                if searched:
                    forced = pending_search_action(s)
                    if forced:
                        auto_progress.add(forced)
                continue
            s.signal("typing", on=False)
            text = guard.output(resp.text.strip(), s)
            if not text:
                raise LLMError("пустой ответ модели")
            await s.say(text, await_reply=True)
            s.messages.append({"role": "assistant", "content": text, **({"functions_state_id": resp.functions_state_id} if resp.functions_state_id else {})})
            s.remember()
            turn.finish("await_reply")
        except (LLMError, TurnStopped) as exc:
            reason = exc.reason if isinstance(exc, TurnStopped) else type(exc).__name__
            turn.finish(reason)
            s.signal("typing", on=False)
            s.signal("model_wait", on=False)
            s.push("search_progress", on=False)
            recovery = pending_search_action(s)
            if reason == "repeated_tool" and recovery and s.request_intent in ("search", "refine", "continue") and recovery not in auto_progress:
                s.messages = close_dangling_calls(s.messages, "повтор без прогресса; продолжаю безопасный поиск")
                auto_progress.add(recovery)
                forced = recovery
                turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
                continue
            if isinstance(exc, understanding.UnderstandingError):
                text = "Не удалось надёжно проверить условия. Уточните запрос — прежние условия сохранены."
            elif isinstance(exc, LLMBusy):
                text = "Сейчас много обращений, модель занята. Напишите чуть позже — продолжу с сохранёнными условиями."
            elif isinstance(exc, LLMError):
                s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=True, reason="Модель временно недоступна")
                text = f"Модель {s.llm.name} сейчас не отвечает. Напишите, когда попробовать снова — продолжу с того же места."
            elif reason == "repeated_tool":
                text = "Заметил повтор одного и того же действия и остановился. Напишите «продолжить» или уточните запрос — условия сохранены."
            elif reason == "tool_limit":
                text = "Достигнут лимит действий за один ход. Напишите, чтобы продолжить с сохранёнными условиями."
            else:
                text = "Обработка заняла слишком много времени, поэтому я остановился. Напишите «продолжить» — условия поиска сохранены."
            if interrupted_call == "call_seller" and s.current:
                s.push("call_end", status="failed", price=s.current["listing"]["price"], learned={}, viewing=None)
                s.current = None
            # В том числе закрываем ещё не выполненные вызовы из одного ответа модели.
            s.messages = close_dangling_calls(s.messages, "прервано: обработка остановлена; требуется новое сообщение клиента")
            await s.say(text, await_reply=True)
            s.messages.append({"role": "assistant", "content": text})
            s.remember()
        s.messages.append({"role": "user", "content": await s.inbox.get()})
        turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
        auto_progress.clear()
        s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=False)


async def run_session(s) -> None:
    try:
        await run(s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # показать ошибку в интерфейсе, а не молча упасть
        record("session_error", session=s.id, error_type=type(exc).__name__)
        s.push("error", text="Ошибка агента. Попробуйте продолжить диалог.")
