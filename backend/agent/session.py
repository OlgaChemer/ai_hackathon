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
from copy import copy, deepcopy

from .. import db
from ..config import settings
from ..runtime import TurnStopped, human_wait
from ..db import repo
from ..llm import LLMClient
from . import finance as F
from . import preferences as P
from . import guard, qa, validation
from .numbers import fmt
from . import state as durable_state, persistence


class Superseded(TurnStopped):
    def __init__(self):super().__init__('superseded')

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
    requested_action: str | None = None
    research_query: dict | None = None
    guidance: dict = field(default_factory=dict)
    client_revision: int = 0  # только принятые сообщения/решения клиента
    seq: int = 0                      # номер последнего события диалога
    history: list[dict] = field(default_factory=list)  # события этого запуска (если база недоступна)
    subscribers: set[asyncio.Queue] = field(default_factory=set)  # открытые вкладки
    idle_since: float = field(default_factory=time.monotonic)  # когда закрылась последняя вкладка
    pending: dict[str, asyncio.Future] = field(default_factory=dict)  # карточки, которые ждут решения клиента
    pending_ids: dict[str, str] = field(default_factory=dict)
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    activity: str = 'working'
    inbox: asyncio.Queue = field(default_factory=lambda:asyncio.Queue(maxsize=settings.command_queue_capacity))
    seller_queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # реплики живого продавца
    task: asyncio.Task | None = None
    messages: list[dict] = field(default_factory=list)  # память: разговор агента с моделью (формат OpenAI)
    status: dict[int, str] = field(default_factory=dict)  # шаги степпера: номер -> active | done

    voice_mode: bool = False           # стиль ответа; не параметр подбора и не согласие

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
    selection_query: dict = field(default_factory=dict)
    selection_scope_ids: list[str] = field(default_factory=list)
    selection_dirty: bool = False
    selection_operation: str = 'rank'
    selection_profile_stale: bool = False
    last_issue: dict | None = None
    call_targets: list[str] = field(default_factory=list)
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
    durable: bool = False
    state_version: int = 0
    profile_revision: int = 0
    selection_revision: int = 0
    pending_decisions: dict = field(default_factory=dict)
    operations: dict = field(default_factory=dict)
    active_operation: dict | None = None
    qa_pending: list = field(default_factory=list)
    started: bool = False
    _durable_snapshot: dict | None = None
    _owner: Any = None
    _event_buffer: list = field(default_factory=list)
    _batch_depth: int = 0
    _command: dict | None = None
    _fork_revision: int = 0
    _fork_base: dict | None = None
    command_queue: asyncio.Queue = field(default_factory=lambda:asyncio.Queue(maxsize=settings.command_queue_capacity))
    command_futures: dict = field(default_factory=dict)
    command_task: asyncio.Task | None = None
    review_task: asyncio.Task | None = None
    review_task_seq: int = 0
    _replaying_operation: bool = False

    def fork(self):
        """Инструмент работает на собственном снимке, а не меняет живой контекст."""
        child=copy(self)
        durable_state.restore(child,durable_state.snapshot(self))
        child._owner=self
        child._fork_revision=self.client_revision
        child._fork_base=durable_state.snapshot(child)['data']
        child._event_buffer=[]
        child._batch_depth=0
        return child

    def flush(self, result=None):
        if self._owner is None:
            if not self._batch_depth:persistence.commit(self,command=self._command)
            return
        owner=self._owner
        if owner.client_revision!=self._fork_revision:
            op=owner.active_operation or {}
            if op.get('name')=='call_seller' and op.get('status') in ('external_started','external_completed'):
                # Итог уже начатого звонка — факт конкретной операции, не новая выдача.
                self._fork_revision=owner.client_revision
            else:raise Superseded()
        data=durable_state.snapshot(self)['data']
        # Служебные данные входа и журнал команд принадлежат владельцу.
        protected={'request_inputs','request_review','request_review_seq','client_revision',
                   'pending_decisions','operations','active_operation','qa_pending','started','messages'}
        with persistence.atomic(owner):
            merged=durable_state.snapshot(owner)
            for key,value in data.items():
                if key not in protected and value!=self._fork_base.get(key):merged['data'][key]=value
            before={k:v for k,v in owner.profile.items() if not k.startswith('_') and k not in ('summary','market')}
            after={k:v for k,v in merged['data']['profile'].items() if not k.startswith('_') and k not in ('summary','market')}
            if before!=after:
                merged['data']['profile_revision']=max(merged['data']['profile_revision'],owner.profile_revision+1)
            durable_state.restore(owner,merged)
            if result is not None and owner.active_operation:
                operation=owner.active_operation
                owner.operations[operation['id']]={**operation,'status':'completed','result':deepcopy(result)}
                if len(owner.operations)>100:owner.operations=dict(list(owner.operations.items())[-100:])
                owner.active_operation=None
                owner.pending_decisions={k:t for k,t in owner.pending_decisions.items() if t.get('status')!='consumed'}
            for ev in self._event_buffer:
                owner.push(ev['type'],**{k:v for k,v in ev.items() if k!='type'})
        self._event_buffer.clear()
        self._fork_base=data

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
        financial=self.programs_key != F.programs_key(self.profile)
        self.search = None
        self.invalidate_decisions(financial=financial)

    def invalidate_decisions(self, *, purchase=True, financial=False):
        """Смена условий отзывает прежний выбор/план, но сохраняет историю процесса."""
        had_purchase=bool(self.calls or self.agreed or self.chosen or self.current or self.journal or self.status.get(8)=='done')
        had_finance=bool(self.programs_result or self.mortgage or self.finance or self.status.get(2))
        if financial:
            self.programs_key = self.programs_result = None
            self.profile.pop('_programs_cache',None)
            self.mortgage = self.finance = None
            self.status.pop(2,None)
        if purchase:
            self.calls.clear();self.agreed.clear();self.chosen=self.current=None
            self.call_targets=[];self.journal.clear()
        first=3 if purchase else 8
        self.status={k:v for k,v in self.status.items() if k<first}
        kinds=('call','price','object','sign') if purchase else ('sign',)
        self.pending_decisions={k:t for k,t in self.pending_decisions.items() if k not in kinds}
        self.stage='Запрос' if purchase else 'Финансы'
        if had_purchase or (financial and had_finance):
            self.push('purchase_reset',clear_steps=([2] if financial else [])+list(range(5 if purchase else 8,10)),
                      kinds=list(kinds),text='Условия изменились: прежние решения по объекту и план нужно пересмотреть.' if purchase and had_purchase
                      else 'Финансирование изменилось: прежний план покупки нужно пересчитать.')

    # ------------------------------------------------------------- лента событий
    def push(self, type_: str, **payload: Any) -> dict:
        """Событие в ленту диалога: номер, база, все открытые вкладки. Синхронно — порядок не путается."""
        payload = guard.output(payload, self, limit=False)
        if type_ in ("message", "turn", "confirm") and "text" in payload:
            payload["text"] = guard.output(payload["text"], self)
        if self._owner is not None and type_=='call_end' and self._owner.active_operation:
            self._owner.active_operation.update(status='external_completed',external_result=_jsonable(payload))
        return persistence.push(self,type_,_jsonable(payload))

    def signal(self, type_: str, **payload: Any) -> None:
        """Сигнал открытым вкладкам без записи в историю: «агент думает»."""
        if self._owner is not None:
            if self._owner.client_revision==self._fork_revision:self._owner.signal(type_,**payload)
            return
        if type_ == 'typing':
            self.activity = 'working' if payload.get('on') else ('awaiting_user' if self.pending else 'idle')
        from ..events import deliver
        deliver(self,{"type":type_,**payload,"seq":0})

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
        if self._owner is not None:
            self.flush()
            answer=await self._owner.ask(kind,title,text,**payload)
            self._fork_revision=self._owner.client_revision
            self.active_operation=deepcopy(self._owner.active_operation)
            self._replaying_operation=False
            return answer
        self._replaying_operation=False
        ticket=self.pending_decisions.get(kind)
        if ticket and ticket.get('status')=='answered':
            ticket['status']='consumed'
            return deepcopy(ticket['answer'])
        if ticket and ticket.get('status')=='waiting':
            title,text,payload=ticket['title'],ticket['text'],ticket['payload']
        else:
            ticket={'kind':kind,'title':title,'text':text,'payload':_jsonable(payload),
                    'decision_id':uuid.uuid4().hex[:16],'status':'waiting',
                    'operation_id':(self.active_operation or {}).get('id')}
            self.pending_decisions[kind]=ticket
        if self.active_operation:self.active_operation['status']='waiting'
        fut = asyncio.get_running_loop().create_future()
        self.pending[kind] = fut
        decision_id = ticket['decision_id']
        self.pending_ids[kind] = decision_id
        self.signal('typing',on=False)
        self.signal('model_wait',on=False)
        if not ticket.get('published'):
            ticket['published']=True
            self.push("confirm", kind=kind, title=title, text=text, decision_id=decision_id, **payload)
        else:self.remember()
        try:
            with human_wait():
                answer=await fut
                ticket['status']='consumed'
                if self.active_operation:self.active_operation['status']='running'
                return answer
        finally:
            if self.pending.get(kind) is fut:
                self.pending.pop(kind, None)
                self.pending_ids.pop(kind,None)

    def resolve(self, kind: str, approved: bool | None, data: dict | None, decision_id: str | None = None) -> bool:
        """Решение клиента. В ленту — resolved с подписью кнопки (для истории) и фраза, если решение сказано голосом."""
        fut = self.pending.get(kind)
        ticket=self.pending_decisions.get(kind)
        if (not fut or fut.done()) and not (ticket and ticket.get('status')=='waiting'):
            return False
        current_id=(ticket or {}).get('decision_id') or self.pending_ids.get(kind)
        if decision_id is not None and decision_id != current_id:
            return False
        data = dict(data or {})
        try:
            if approved is not None and type(approved) is not bool:
                raise ValueError("Нужно явное решение клиента")
            validation.confirmation(kind, data)
        except ValueError as exc:
            raise guard.InputRejected(str(exc)) from exc
        if kind=='call' and approved and data.get('mode')=='phone':
            from .. import phone_access
            if not db.required(phone_access.confirmation,data.get('phone_grant_id'),self.id,
                    (self.active_operation or {}).get('id'),current_id):
                raise guard.InputRejected('Телефонное разрешение не относится к текущей квартире или уже использовано.')
        if self.refuse(data, "confirmation"):
            raise guard.InputRejected("Ответ отклонён. Карточка остаётся открытой — измените текст.")
        if data.get("text") and (approved is None or kind == "clarify"):
            # /confirm тоже может передать текстовый ответ; receive_text уже записал его сам.
            if not self.request_inputs or self.request_inputs[-1]['text'] != data['text'] or self.request_inputs[-1]['seq'] <= self.request_review_seq:
                event = self.push("message", role="user", text=data['text'])
                self.accept_request(event['text'], event['seq'])
        with persistence.atomic(self):
            self.client_revision += 1
            label, said = data.pop("_label", None), data.pop("_said", None)
            if said:self.push("message", role="user", text=str(said)[:500])
            answer={"approved":approved,"data":data}
            if ticket:
                ticket['status']='answered';ticket['answer']=answer
            if self._command:
                self._command['result']=({'handled_as':'answer'} if self._command['kind']=='message' else {'resolved':True})
            self.push("resolved", kind=kind, approved=approved, label=label, decision_id=current_id)
        if fut and not fut.done():fut.set_result(answer)
        return True

    def accept_request(self, text, seq):
        question = next((e for e in reversed(self.history)
                         if e.get("type") == "confirm" and e.get("kind") in self.pending), {})
        self.request_inputs.append({"seq": seq, "text": text, "question": question.get('text',''),
                                    "options": question.get('chips') or [],
                                    "question_field":((self.active_operation or {}).get('arguments') or {}).get('field')})
        self.request_review = None

    async def receive_text(self, text: str, marks: dict | None = None, command_id: str | None = None) -> str:
        from .commands import submit
        result=await submit(self,'message',{'text':text,'marks':marks},command_id)
        return result['handled_as']

    async def receive_decision(self,kind,approved,data,decision_id=None,command_id=None):
        from .commands import submit
        result=await submit(self,'confirm',{'kind':kind,'approved':approved,'data':data,'decision_id':decision_id},command_id)
        return result['resolved']

    async def _receive_text(self, text: str, marks: dict | None = None) -> str:
        progress=(self._command or {}).setdefault('progress',{})
        with persistence.atomic(self):
            if not progress.get('message_seq'):
                ev=self.push('message',role='user',text=text,command_id=(self._command or {}).get('id'))
                progress['message_seq']=ev['seq']
                if self.refuse(text):return 'blocked'
                from .selection_request import set_visible_scope
                if not set_visible_scope(self,marks):
                    self.push('message',role='agent',text='Подборка уже обновилась. Посмотрите актуальную страницу и повторите запрос — прежние условия сохранены.',await_reply=True)
                    return 'unresolved'
                self.accept_request(ev['text'],ev['seq'])
                self.client_revision+=1
            elif not any(x['seq']==progress['message_seq'] for x in self.request_inputs):
                self.accept_request(text,progress['message_seq'])
        if self.request_inputs:
            from . import understanding
            from ..llm import LLMError
            try:
                review = await understanding.prepare_from_http(self)
            except Superseded:
                # Новая команда уже в состоянии. Это отмена старой работы,
                # а не ошибка клиента; failed_input не должен удалить новый ввод.
                if not self.inbox.full():self.inbox.put_nowait(text)
                return 'message'
            except (LLMError, TurnStopped) as exc:
                await self.say(understanding.failed_input(self,exc), await_reply=True)
                return "unresolved"
            if review and review['intent'] == 'question' and review['decision'] != 'clarify' and not review.get('requested_action'):
                self.request_review_seq = review['seq']
                from .commands import start_questions
                if len(self.qa_pending)>=settings.question_queue_capacity:
                    with persistence.atomic(self):
                        self.push('message',role='agent',text='В очереди уже много вопросов. Этот вопрос не принят к обработке; повторите его после ответа на предыдущие.',
                                  in_reply_to=(self._command or {}).get('id'),outcome='capacity')
                        if self._command:self._command['result']={'handled_as':'unresolved'}
                    return 'unresolved'
                self.qa_pending.append({'id':(self._command or {}).get('id') or uuid.uuid4().hex,'text':text})
                if self._command:self._command['result']={'handled_as':'question'}
                self.remember()
                start_questions(self)
                return "question"
            self.client_revision += 1
            kind = next(iter(self.pending), None) or next((k for k,t in self.pending_decisions.items() if t['status']=='waiting'),None)
            if kind:
                data = {"text": text}
                if kind == "selection":
                    data.update(action="text", liked=(marks or {}).get("liked", []), disliked=(marks or {}).get("disliked", []))
                if self.resolve(kind, None, data):
                    return "answer"
        self.client_revision += 1
        if not self.inbox.full():self.inbox.put_nowait(text)
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
        if self._owner is None:
            if not self._batch_depth:persistence.commit(self,command=self._command)

    def checkpoint_operation(self, status, **data):
        if self._owner is not None:self.flush()
        owner=self._owner or self
        if owner.active_operation:
            owner.active_operation.update(status=status,**deepcopy(data))
            owner.remember()
            self.active_operation=deepcopy(owner.active_operation)


SESSIONS: dict[str, Session] = {}


def cache_session(s):
    if s.id not in SESSIONS and len(SESSIONS)>=settings.max_cached_dialogues:
        idle=[old for old in SESSIONS.values() if not running(old) and not old.subscribers
              and (old.command_task is None or old.command_task.done()) and (old.qa_task is None or old.qa_task.done())]
        if not idle:raise db.CommandCapacityError('Обработчики заняты. История сохранена; повторите позже.')
        SESSIONS.pop(min(idle,key=lambda old:old.idle_since).id,None)
    SESSIONS[s.id]=s


def create_session(query: str, llm: LLMClient, visitor_id: str | None = None, user_id: int | None = None) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], query=query.strip(), llm=llm, visitor_id=visitor_id, user_id=user_id)
    db.required(repo.create_chat, s.id, guard.output(s.query), visitor_id, user_id)
    s.durable=True
    s.remember()
    cache_session(s)
    return s


def restore_session(chat_id: str, text: str, llm: LLMClient) -> Session:
    """Продолжить диалог, агент которого остановлен (перезапуск сервера, долгая пауза): та же лента и память."""
    from ..telemetry import count
    count('restored')
    row=db.required(repo.get_chat,chat_id)
    info=(row.profile,row.memory,row.visitor_id,row.user_id,row.last_seq) if row else None
    profile, memory, vid, uid, last_seq = info or (None, None, None, None, 0)
    s = Session(id=chat_id, query=text.strip(), llm=llm, visitor_id=vid, user_id=uid, seq=last_seq or 0)
    s.durable=True
    s.state_version=(row.state_version or 0) if row else 0
    if row and row.state and row.state.get('data',{}).get('profile')==dict(profile or {}) and row.state.get('data',{}).get('messages')==list(memory or []):
        durable_state.restore(s,row.state)
        s.messages=close_dangling_calls(s.messages,completed=s.operations,
                                       active_id=(s.active_operation or {}).get('id'))
        restore_approval(s)
        if s.programs_result is not None and s.programs_key!=F.programs_key(s.profile):
            s.programs_key=s.programs_result=None
            s.mortgage=s.finance=None
            s.profile.pop('_programs_cache',None)
        if text.strip():s.query=text.strip();s.started=False
        s._durable_snapshot=durable_state.snapshot(s)
        cache_session(s)
        return s
    s.profile = dict(profile or {})
    s.last_issue=s.profile.get('_last_issue')
    s.requested_action=s.profile.get('_requested_action')
    s.selection_profile_stale=bool(s.profile.get('_selection_stale'))
    s.selection_query = s.profile.get('_selection_query') or {}
    previous=db.safe(repo.last_nonempty_selection,chat_id)
    if previous:
        s.shown=previous['cards']
        s.selection_scope_ids=[x['id'] for x in s.shown]
        s.selection_query=previous.get('query') or s.selection_query
        s.profile['_selection_query']=dict(s.selection_query)
        s.prefs.liked=list(previous.get('liked') or [])
        s.prefs.disliked=list(previous.get('disliked') or [])
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
        if tuple(cache['key'])==F.programs_key(s.profile):
            s.programs_key,s.programs_result=tuple(cache['key']),cache['result']
    s.prefs.sync(s.profile)
    cache_session(s)
    db.safe(repo.update_chat, chat_id, status="running")
    return s


def restore_approval(s):
    if '_approved_budget_max' not in s.profile:
        names={};approved=None
        for m in s.messages:
            for call in m.get('tool_calls') or []:names[call['id']]=call['function']['name']
            if m.get('role')!='tool':continue
            try:r=json.loads(m.get('content','{}'))
            except (TypeError,ValueError):continue
            if not isinstance(r,dict):continue
            if names.get(m.get('tool_call_id'))=='confirm_red_lines' and 'approved' in r:
                approved=r.get('budget_max') if r['approved'] else None
            if names.get(m.get('tool_call_id'))=='update_request' and approved:
                value=(r.get('profile') or {}).get('budget_max')
                if value is not None and value!=approved:approved=None
        s.profile['_approved_budget_max']=approved
    s.red_lines_ok=bool(s.profile.get('_approved_budget_max') and s.profile['_approved_budget_max']==s.profile.get('budget_max'))


def close_dangling_calls(messages: list[dict], reason: str = "прервано: агент перезапускался, клиент не успел ответить", *, completed=None, active_id=None) -> list[dict]:
    """Действие было начато, но сервер остановился раньше результата: даём модели честный результат «прервано»,
    иначе разговор с вызовом без ответа модель не примет."""
    out: list[dict] = []
    for i, m in enumerate(messages):
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in messages[i + 1:] if x.get("role") == "tool"}
            for call in m["tool_calls"]:
                if call["id"] not in answered:
                    if call['id']==active_id:continue
                    operation=(completed or {}).get(call['id']) or {}
                    content=operation['result'] if operation.get('status')=='completed' else {'error':reason}
                    out.append({"role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps(content, ensure_ascii=False)})
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
