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
from .. import db
from ..llm import LLMError, LLMResponse, ToolCall
from ..llm.base import LLMBusy
from ..config import settings
from ..runtime import Turn, TurnStopped, record
from . import actions, guard, prompts, understanding
from .features import FEATURES
from .numbers import fmt
from .profile import missing_required
from .workflow import next_search_action,available
from .session import close_dangling_calls
from .session import Superseded

MAX_ACTIONS_IN_ROW = 14  # действий подряд без ответа клиенту — дальше модель должна заговорить с клиентом


def state_brief(s) -> str:
    """Что уже известно и сделано — модель видит это каждый ход."""
    p = s.profile
    lines = []
    if p:
        known = {k: v for k, v in p.items() if not k.startswith("_") and k not in ("summary", "market") and v not in (None, [], "")}
        if known.get("features"):
            known["features"] = [FEATURES[k][0] for k in known["features"]]
        if known.get("poi"):
            known["poi"] = [geo.POI[k][0] for k in known["poi"]]
        lines.append("Запрос: " + json.dumps(known, ensure_ascii=False))
        from .profile import missing_search
        missing = missing_search(p)
        lines.append("Не хватает для поиска: " + (", ".join(missing) if missing else "ничего"))
    else:
        lines.append("Запрос ещё не записан.")
    lines.append("Рамки клиентом " + ("подтверждены." if s.red_lines_ok else "НЕ подтверждены."))
    if s.market:
        lines.append('Ценовые ориентиры уже показаны: '+json.dumps(s.market,ensure_ascii=False))
    if s.mortgage:
        chosen = next(c for c in s.mortgage["programs"] if c["key"] == s.mortgage["chosen"])
        lines.append(f"Ипотека посчитана: {chosen['title']}, {chosen['rate'] * 100:.1f}%, платёж {fmt(chosen['monthly'])}.")
    if s.finance:
        lines.append(f"План накоплений: не хватает {fmt(s.finance['gap'])}, срок {s.finance['months']} мес.")
    if s.search:
        lines.append(f"Поиск: найдено {len(s.search['listings'])}, в бюджете {len(s.search['pool'])}.")
        from .coverage import report
        coverage=report(s)
        lines.append('Охват: '+coverage['summary']+' Причины неполноты: '+', '.join(coverage['reasons']))
    if s.shown:
        cards = "; ".join(f"{i + 1}) {x['id']} — {x.get('address')}, {fmt(x['price'])}" for i, x in enumerate(s.shown))
        lines.append(f"Последняя подборка: {cards}. Нравятся: {', '.join(s.prefs.liked) or 'ничего'}.")
    for lid, c in s.calls.items():
        lines.append(f"Звонок {lid}: {'договорились' if c['status'] == 'agreed' else 'не договорились'} на {fmt(c['price'])}"
                     + (", цену подтвердил клиент" if lid in s.agreed else "") + (", квартира выбрана" if s.chosen == lid else ""))
    if s.request_intent:
        lines.append("Проверенное намерение клиента: " + s.request_intent + ".")
    if s.requested_action:
        from .capabilities import decision
        lines.append('Запрошено действие: '+s.requested_action+'. '+(decision(s,s.requested_action)[1] or 'Выполнить сейчас.'))
    if s.call_targets:
        lines.append('Клиент просил звонить только по ID: ' + ', '.join(s.call_targets))
    if s.selection_query:
        lines.append('Проверенный запрос к подборке: ' + json.dumps(s.selection_query, ensure_ascii=False))
    if s.profile.get('seller_questions'):
        lines.append('Дополнительные вопросы продавцу: ' + json.dumps(s.profile['seller_questions'],ensure_ascii=False))
    if s.profile.get('_unsupported_conditions'):
        lines.append('Система НЕ проверяет эти пожелания: ' + json.dumps(s.profile['_unsupported_conditions'],ensure_ascii=False))
    if s.last_issue:
        lines.append('Последняя ошибка системы: '+s.last_issue['message'])
    return "\n".join(lines)


async def think(s, with_actions: bool):
    messages = [{"role": "system", "content": prompts.AGENT_SYSTEM + ("\n" + prompts.VOICE_STYLE if s.voice_mode else "") + "\n" + guard.POLICY},
                guard.data_message("session_state", state_brief(s)), *guard.output(s.messages, limit=False)]
    return await s.llm.chat(messages, tools=actions.tools(s) if with_actions else None, temperature=0.3, max_tokens=900,
                            tag="agent")



async def run(s) -> None:
    """Агент работает, пока открыт диалог: отвечает, действует, ждёт клиента. Остановить — отменить задачу."""
    s.push("provider", provider=s.llm.name, model=s.llm.model, degraded=False)
    if s.query and not s.started:
        from .persistence import atomic
        blocked=False
        with atomic(s):
            s.started=True
            initial = s.push('message',role='user',text=s.query)
            blocked=bool(s.refuse(s.query))
            if not blocked:
                s.accept_request(initial['text'],initial['seq'])
                s.messages.append({'role':'user','content':s.query})
        if blocked:s.messages.append({'role':'user','content':await s.inbox.get()})
    from . import commands
    commands.resume(s)
    if s.active_operation is None:
        ticket=next((t for t in s.pending_decisions.values() if t['kind']=='clarify' and t['status'] in ('waiting','answered')),None)
        if ticket:
            answer=await s.ask(ticket['kind'],ticket['title'],ticket['text'],**ticket['payload'])
            s.messages.extend([{'role':'assistant','content':ticket['text']},
                               {'role':'user','content':answer['data'].get('text','')}])
            s.pending_decisions.pop(ticket['kind'],None)
            s.remember()
    turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
    active=s.active_operation
    forced=None
    if active and active['status'] in ('waiting','running','external_started','external_completed'):
        if active['name']=='call_seller' and active['status'] in ('running','external_started'):
            s.call_targets=[]
            s.request_intent='hold'
            s.last_issue={'kind':'external_outcome_unknown','message':'Звонок был прерван. Его исход нужно проверить; автоматически повторять звонок не буду.'}
            await s.say(s.last_issue['message'],await_reply=True)
            s.operations[active['id']]={**active,'status':'needs_review'}
            s.active_operation=None
            s.pending_decisions={k:t for k,t in s.pending_decisions.items() if t.get('operation_id')!=active['id']}
            s.remember()
        else:
            forced=ToolCall(active['name'],active['arguments'],active['id'])
            s._replaying_operation=True
    auto_progress = set()
    while True:
        s.signal("typing", on=True)
        interrupted_call = None
        try:
            # Ответ в открытой карточке уже проверен HTTP-обработчиком: применяем к границе хода.
            if s.request_review and s.request_review['seq']>s.request_review_seq:
                checked = await turn.run(lambda: understanding.ensure(s), kind="tool", name="review_input")
                if checked and checked.get("needs_clarification"):
                    s.messages.extend([{"role":"assistant","content":checked['question']},
                                       {"role":"user","content":checked['client_said']}])
                    s.remember()
                    continue
            from .capabilities import POLICIES
            if s.requested_action and not available(s,s.requested_action) and not (POLICIES.get(s.requested_action) and POLICIES[s.requested_action].information):
                s.requested_action=None;s.profile.pop('_requested_action',None);s.remember()
            intake = next_search_action(s)
            if not isinstance(forced,ToolCall) and (intake in ("confirm_red_lines", "ask_payment") or (intake and intake==s.requested_action)):
                forced = intake
            if forced:
                recovered=forced if isinstance(forced,ToolCall) else ToolCall(forced, {}, f"progress_{uuid.uuid4().hex[:8]}")
                resp = LLMResponse('',[recovered])
                record('search_recovery',session=s.id,action=recovered.name)
                forced = None
            else:
                model_revision=s.client_revision
                resp = await turn.run(lambda: think(s, with_actions=turn.tool_calls < MAX_ACTIONS_IN_ROW),
                                      kind="llm", name="agent")
                if model_revision!=s.client_revision:raise Superseded()
            if not resp.tool_calls and any(x['seq'] > s.request_review_seq for x in s.request_inputs):
                checked = await turn.run(lambda: understanding.ensure(s), kind="tool", name="review_input")
                if checked and checked.get("needs_clarification"):
                    s.messages.extend([{"role":"assistant","content":checked['question']},
                                       {"role":"user","content":checked['client_said']}])
                    s.remember()
                    continue
            next_action = next_search_action(s)
            if not resp.tool_calls and next_action and next_action not in auto_progress and turn.tool_calls < MAX_ACTIONS_IN_ROW:
                s.messages.append({"role": "assistant", "content": guard.output(resp.text or "", s),
                    **({"functions_state_id": resp.functions_state_id} if resp.functions_state_id else {})})
                auto_progress.add(next_action)
                forced = next_action
                continue
            if resp.tool_calls:
                if len(resp.tool_calls) > MAX_ACTIONS_IN_ROW - turn.tool_calls:
                    raise TurnStopped("tool_limit")
                calls = [c if c.id else dataclasses.replace(c, id=f"call_{uuid.uuid4().hex[:8]}") for c in resp.tool_calls]
                old_ids={c['id'] for m in s.messages for c in m.get('tool_calls') or []}
                resuming=s._replaying_operation and all(c.id in old_ids for c in calls)
                if not resuming:s.messages.append({"role": "assistant", "content": guard.output(resp.text or "", s), "tool_calls": [
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
                    if call.name == "update_request" and result.get("already_reviewed"):
                        searched = True
                    s.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(guard.output(guard.sanitize(result), limit=False), ensure_ascii=False, default=str)})
                    if s.client_revision != revision:
                        turn.finish("client_answer")
                        auto_progress.clear()
                        turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
                s.remember()
                if searched:
                    forced = next_search_action(s)
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
        except Superseded:
            s.messages=close_dangling_calls(s.messages,'Результат устарел: поступило новое сообщение клиента.')
            s.signal('typing',on=False)
            s.signal('model_wait',on=False)
            s.push('search_progress',on=False)
            # Ввод уже сохранён в request_inputs; не ждём повторной отправки.
            while not s.inbox.empty():
                s.messages.append({'role':'user','content':s.inbox.get_nowait()})
            s.remember()
            turn=Turn(s.id,settings.agent_turn_timeout_s,s.signal)
            forced='update_request';auto_progress.clear()
            continue
        except (LLMError, TurnStopped) as exc:
            reason = exc.reason if isinstance(exc, TurnStopped) else type(exc).__name__
            turn.finish(reason)
            s.signal("typing", on=False)
            s.signal("model_wait", on=False)
            s.push("search_progress", on=False)
            recovery = next_search_action(s)
            if reason == "repeated_tool" and recovery and recovery not in auto_progress:
                s.messages = close_dangling_calls(s.messages, "повтор без прогресса; продолжаю безопасный поиск")
                auto_progress.add(recovery)
                forced = recovery
                turn = Turn(s.id, settings.agent_turn_timeout_s, s.signal)
                continue
            if isinstance(exc, understanding.UnderstandingError):
                text = understanding.failed_input(s,exc)
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
    except db.PersistenceError:
        s.signal('error',error_kind='persistence',text='Не удалось сохранить состояние. Успех не подтверждён; повторите отправку после восстановления соединения.')
    except Exception as exc:  # показать ошибку в интерфейсе, а не молча упасть
        record("session_error", session=s.id, error_type=type(exc).__name__)
        try:s.push('error',error_kind='internal',text='Ошибка агента. Сохранённый диалог можно продолжить.')
        except db.PersistenceError:s.signal('error',error_kind='persistence',text='Не удалось сохранить состояние диалога.')
