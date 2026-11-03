"""Сессия агента: лента событий диалога, решения клиента, память разговора с моделью, состояние сделки.

Каждое событие (сообщение, карточка, шаг, реплика звонка) получает номер, пишется в ленту диалога в базе
и уходит во все открытые вкладки (SSE). Закрытая вкладка агента не останавливает: диалог открывается снова —
сначала история, потом живые события. Память разговора с моделью тоже в базе: после перезапуска сервера
агент продолжает с того же места.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..runtime import TurnStopped, human_wait
from ..db import repo
from ..llm import LLMClient
from . import finance as F
from . import preferences as P
from . import guard, qa, validation
from .numbers import fmt

STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"]
MEMORY_LIMIT = 80  # сообщений разговора с моделью; старое отрезаем по границе сообщения клиента


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, ensure_ascii=False, default=str))


@dataclass
class Session:
    id: str
    query: str
    llm: LLMClient
    visitor_id: str | None = None
    user_id: int | None = None
    request_inputs: list[dict] = field(default_factory=list)
    request_review_seq: int = 0
    request_review: dict | None = None
    request_intent: str | None = None
    client_revision: int = 0  # только принятые сообщения/решения клиента
    seq: int = 0                      # номер последнего события диалога
    history: list[dict] = field(default_factory=list)  # события этого запуска (если база недоступна)
    subscribers: set[asyncio.Queue] = field(default_factory=set)  # открытые вкладки
    idle_since: float = field(default_factory=time.monotonic)  # когда закрылась последняя вкладка
    pending: dict[str, asyncio.Future] = field(default_factory=dict)  # карточки, которые ждут решения клиента
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)       # сообщения клиента агенту
    seller_queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # реплики живого продавца
    task: asyncio.Task | None = None
    messages: list[dict] = field(default_factory=list)  # память: разговор агента с моделью (формат OpenAI)
    status: dict[int, str] = field(default_factory=dict)  # шаги степпера: номер -> active | done

    # --- состояние сделки (его меняют действия агента)
    profile: dict = field(default_factory=dict)
    red_lines_ok: bool = False          # клиент подтвердил рамки: без этого ни поиска, ни звонка
    prefs: P.Prefs = field(default_factory=P.Prefs)
    market: dict | None = None
    finance: dict | None = None
    mortgage: dict | None = None
    programs_key: tuple | None = None
    programs_result: dict | None = None
    search: dict | None = None          # результат поиска: listings, pool, meta, comps, report, map_ok, pages
    shown: list = field(default_factory=list)       # последняя подборка (карточки)
    calls: dict[str, dict] = field(default_factory=dict)     # итоги звонков по id объявления
    agreed: dict[str, int] = field(default_factory=dict)     # цена, которую клиент подтвердил
    chosen: str | None = None           # квартира, которую клиент выбрал после просмотра
    current: dict | None = None         # квартира, по которой идёт звонок: {"listing", "fair"}
    journal: dict = field(default_factory=dict)
    opened: dict[str, int] = field(default_factory=dict)
    map_warned: bool = False
    stage: str | None = None

    blocked: list[float] = field(default_factory=list)
    qa_task: asyncio.Task | None = None

    def blocked_recently(self, window: float = 120) -> int:
        now = time.monotonic()
        self.blocked = [t for t in self.blocked if now - t < window]
        return len(self.blocked)

    def refuse(self, value, where: str = "input") -> str | None:
        verdict = guard.input_verdict(value)
        if verdict.ok:
            return None
        self.blocked_recently()
        self.blocked.append(time.monotonic())
        guard.note(self, where, verdict.category, json.dumps(value, ensure_ascii=False))
        self.push("security", category=verdict.category)
        self.push("message", role="agent", text=verdict.reply)
        return verdict.category

    def invalidate_deal(self, *, keep_limit=False):
        """Согласия относятся к прежним условиям и не могут разрешать новую сделку."""
        if not keep_limit:
            self.red_lines_ok = False
        from .actions.money import programs_key
        if self.programs_key != programs_key(self):
            self.programs_key = self.programs_result = None
            self.profile.pop('_programs_cache',None)
            self.mortgage = self.finance = None
        self.search = None
        self.calls.clear()
        self.agreed.clear()
        self.chosen = self.current = None
        self.shown = []
        self.journal.clear()

    # ------------------------------------------------------------- лента событий
    def push(self, type_: str, **payload: Any) -> dict:
        """Событие в ленту диалога: номер, база, все открытые вкладки. Синхронно — порядок не путается."""
        self.seq += 1
        payload = guard.output(payload, self, limit=False)
        if type_ in ("message", "turn", "confirm") and "text" in payload:
            payload["text"] = guard.output(payload["text"], self)
        ev = _jsonable({"type": type_, **payload, "seq": self.seq})
        self.history.append(ev)
        db.safe(repo.save_event, self.id, self.seq, type_, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)
        if type_ == "done":
            db.safe(repo.update_chat, self.id, status=payload.get("status", "completed"))
        return ev

    def signal(self, type_: str, **payload: Any) -> None:
        """Сигнал открытым вкладкам без записи в историю: «агент думает»."""
        for q in list(self.subscribers):
            q.put_nowait({"type": type_, **payload, "seq": 0})

    async def emit(self, type_: str, **payload: Any) -> None:
        self.push(type_, **payload)

    async def say(self, text: str, *, await_reply: bool = False) -> None:
        self.push("message", role="agent", text=text, **({"await_reply": True} if await_reply else {}))

    async def step(self, n: int) -> None:
        """Шаг степпера: n — активный, все активные до него — сделаны. Агент может вернуться назад (уточнить запрос)."""
        for k, st in list(self.status.items()):
            if k < n and st == "active":
                self.status[k] = "done"
                self.push("step", step=k, title=STEPS[k - 1], status="done")
        if self.status.get(n) != "active":
            self.status[n] = "active"
            self.stage = STEPS[n - 1]
            self.push("step", step=n, title=STEPS[n - 1], status="active")

    async def finish_step(self, n: int) -> None:
        if self.status.get(n) != "done":
            self.status[n] = "done"
            self.push("step", step=n, title=STEPS[n - 1], status="done")

    # ------------------------------------------------------------- решения клиента
    async def ask(self, kind: str, title: str, text: str, **payload: Any) -> dict:
        """Карточка с решением клиента. Ждёт: {"approved": bool | None, "data": {...}}.
        Клиент может вместо кнопки написать текстом — тогда approved = None, а текст в data["text"]."""
        fut = asyncio.get_running_loop().create_future()
        self.pending[kind] = fut
        self.push("confirm", kind=kind, title=title, text=text, **payload)
        try:
            with human_wait():
                return await fut
        finally:
            self.pending.pop(kind, None)

    def resolve(self, kind: str, approved: bool | None, data: dict | None) -> bool:
        """Решение клиента. В ленту — resolved с подписью кнопки (для истории) и фраза, если решение сказано голосом."""
        fut = self.pending.get(kind)
        if not fut or fut.done():
            return False
        data = dict(data or {})
        try:
            if approved is not None and type(approved) is not bool:
                raise ValueError("Нужно явное решение клиента")
            validation.confirmation(kind, data)
        except ValueError as exc:
            raise guard.InputRejected(str(exc)) from exc
        if self.refuse(data, "confirmation"):
            raise guard.InputRejected("Ответ отклонён. Карточка остаётся открытой — измените текст.")
        if data.get("text") and (approved is None or kind == "clarify"):
            # /confirm тоже может передать текстовый ответ; receive_text уже записал его сам.
            if not self.request_inputs or self.request_inputs[-1]['text'] != data['text'] or self.request_inputs[-1]['seq'] <= self.request_review_seq:
                event = self.push("message", role="user", text=data['text'])
                self.accept_request(event['text'], event['seq'])
        self.client_revision += 1
        label, said = data.pop("_label", None), data.pop("_said", None)
        if said:
            self.push("message", role="user", text=str(said)[:500])
        self.push("resolved", kind=kind, approved=approved, label=label)
        fut.set_result({"approved": approved, "data": data})
        return True

    def accept_request(self, text, seq):
        question = next((e for e in reversed(self.history)
                         if e.get("type") == "confirm" and e.get("kind") in self.pending), {})
        self.request_inputs.append({"seq": seq, "text": text, "question": question.get('text',''),
                                    "options": question.get('chips') or []})
        self.request_review = None

    async def receive_text(self, text: str, marks: dict | None = None) -> str:
        ev = self.push("message", role="user", text=text)
        if self.refuse(text):
            return "blocked"
        self.accept_request(ev["text"], ev["seq"])
        if self.pending:
            from . import understanding
            from ..llm import LLMError
            try:
                review = await understanding.prepare_from_http(self)
            except (LLMError, TurnStopped):
                await self.say("Не удалось надёжно разобрать сообщение. Попробуйте ещё раз — текущая карточка и условия сохранены.")
                return "unresolved"
            if review and review['intent'] == 'question' and review['decision'] != 'clarify':
                self.request_review_seq = review['seq']
                if self.qa_task is None or self.qa_task.done():
                    self.qa_task = asyncio.create_task(qa.answer(self, text))
                return "question"
            self.client_revision += 1
            kind = next(iter(self.pending), None)
            if kind:
                data = {"text": text}
                if kind == "selection":
                    data.update(action="text", liked=(marks or {}).get("liked", []), disliked=(marks or {}).get("disliked", []))
                if self.resolve(kind, None, data):
                    return "answer"
        self.client_revision += 1
        self.inbox.put_nowait(text)
        return "message"

    async def open_product(self, product: str, amount: int) -> None:
        if product not in F.PRODUCTS:
            return
        self.opened[product] = int(amount)
        self.push("product_opened", product=product, amount=int(amount))
        await self.say(f"Готово: {F.PRODUCTS[product]['name']} на {fmt(amount)} "
                       "(демо — в проде откроется в СберБанк Онлайн в один клик).")

    # ------------------------------------------------------------- память
    def remember(self) -> None:
        """Сохранить разговор с моделью и профиль: с них диалог продолжится после перезапуска сервера."""
        if len(self.messages) > MEMORY_LIMIT:
            cut = len(self.messages) - MEMORY_LIMIT
            while cut < len(self.messages) and self.messages[cut]["role"] != "user":
                cut += 1
            self.messages = self.messages[cut:]
        db.safe(repo.update_chat, self.id, memory=_jsonable(guard.output(self.messages, limit=False)),
                profile=_jsonable({k: v for k, v in self.profile.items() if k != "market"}) or None)


SESSIONS: dict[str, Session] = {}


def create_session(query: str, llm: LLMClient, visitor_id: str | None = None, user_id: int | None = None) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], query=query.strip(), llm=llm, visitor_id=visitor_id, user_id=user_id)
    SESSIONS[s.id] = s
    db.safe(repo.create_chat, s.id, guard.output(s.query), visitor_id, user_id)
    return s


def restore_session(chat_id: str, text: str, llm: LLMClient) -> Session:
    """Продолжить диалог, агент которого остановлен (перезапуск сервера, долгая пауза): та же лента и память."""
    info = db.safe(lambda d: (lambda c: c and (c.profile, c.memory, c.visitor_id, c.user_id, c.last_seq))(repo.get_chat(d, chat_id)))
    profile, memory, vid, uid, last_seq = info or (None, None, None, None, 0)
    s = Session(id=chat_id, query=text.strip(), llm=llm, visitor_id=vid, user_id=uid, seq=last_seq or 0)
    s.profile = dict(profile or {})
    s.messages = close_dangling_calls(list(memory or []))
    if '_approved_budget_max' not in s.profile:
        # Старый формат: доверяем только результатам серверных инструментов, не репликам.
        names, approved = {}, None
        for message in s.messages:
            for call in message.get('tool_calls') or []:
                names[call['id']] = call['function']['name']
            if message.get('role') != 'tool':continue
            try:result=json.loads(message.get('content','{}'))
            except (TypeError,ValueError):continue
            if not isinstance(result,dict):continue
            if names.get(message.get('tool_call_id'))=='confirm_red_lines' and 'approved' in result:
                approved=result.get('budget_max') if result['approved'] else None
            if names.get(message.get('tool_call_id'))=='update_request' and approved:
                amount=result.get('profile',{}).get('budget_max')
                if amount is not None and amount!=approved:approved=None
        s.profile['_approved_budget_max']=approved
    s.red_lines_ok = bool(s.profile.get('_approved_budget_max') and
                          s.profile['_approved_budget_max'] == s.profile.get('budget_max'))
    cache=s.profile.get('_programs_cache') or {}
    if s.profile.get('payment')=='cash' and cache.get('key') and isinstance(cache.get('result'),dict):
        from .actions.money import programs_key
        if tuple(cache['key'])==programs_key(s):
            s.programs_key,s.programs_result=tuple(cache['key']),cache['result']
    s.prefs.sync(s.profile)
    SESSIONS[chat_id] = s
    db.safe(repo.update_chat, chat_id, status="running")
    return s


def close_dangling_calls(messages: list[dict], reason: str = "прервано: агент перезапускался, клиент не успел ответить") -> list[dict]:
    """Действие было начато, но сервер остановился раньше результата: даём модели честный результат «прервано»,
    иначе разговор с вызовом без ответа модель не примет."""
    out: list[dict] = []
    for i, m in enumerate(messages):
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in messages[i + 1:] if x.get("role") == "tool"}
            for call in m["tool_calls"]:
                if call["id"] not in answered:
                    out.append({"role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps({"error": reason}, ensure_ascii=False)})
    return out


def running(s: Session | None) -> bool:
    return s is not None and s.task is not None and not s.task.done()


IDLE_DONE_S = 30 * 60        # агент остановлен, вкладок нет — убрать из памяти (история и память — в базе)
IDLE_WAITING_S = 6 * 3600    # агент ждёт клиента, а вкладок нет 6 часов — остановить (продолжить можно сообщением)


def reap_idle(now: float | None = None) -> list[str]:
    """Освобождает память от брошенных сессий. Вызывается периодически из main."""
    now = time.monotonic() if now is None else now
    gone = []
    for sid, s in list(SESSIONS.items()):
        if s.subscribers:
            continue
        idle = now - s.idle_since
        if running(s) and idle > IDLE_WAITING_S:
            s.task.cancel()
        elif not running(s) and idle > IDLE_DONE_S:
            gone.append(sid)
            del SESSIONS[sid]
    return gone
