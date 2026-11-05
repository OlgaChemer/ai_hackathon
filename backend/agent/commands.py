"""Долговечная FIFO-очередь входа одного диалога и квитанции повторов."""
import asyncio
import hashlib
import json
import uuid

from .. import db
from ..db import repo
from ..config import settings
from . import guard, persistence


async def submit(s,kind,payload,command_id=None):
    command_id=command_id or uuid.uuid4().hex
    fingerprint=hashlib.sha256(json.dumps([kind,payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    receipt=db.required(repo.accept_command,s.id,command_id,kind,fingerprint,
                        guard.output(payload,s,limit=False),capacity=settings.command_queue_capacity) if s.durable else {
                            'id':command_id,'kind':kind,'payload':payload,'progress':{},'status':'accepted'}
    if receipt['status']=='rejected':raise guard.InputRejected(receipt['result']['error']['message'])
    if receipt['status']=='completed':
        start_questions(s)
        return receipt['result']
    future=enqueue(s,receipt)
    return await asyncio.shield(future)


def enqueue(s,receipt):
    existing=s.command_futures.get(receipt['id'])
    if existing is not None:return existing
    if s.command_queue.full():raise db.CommandCapacityError('Очередь диалога заполнена. Повторите позже.')
    future=asyncio.get_running_loop().create_future()
    # Квитанция остаётся доступна после отключения HTTP-клиента.
    future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    s.command_futures[receipt['id']]=future
    s.command_queue.put_nowait((receipt,future))
    if s.command_task is None or s.command_task.done():s.command_task=asyncio.create_task(drain(s))
    return future


async def drain(s):
    failed=set()
    while not s.command_queue.empty():
        receipt,future=await s.command_queue.get()
        s._command=receipt
        try:
            if receipt.get('result') is not None:result=receipt['result']
            elif receipt['kind']=='message':
                result={'handled_as':await s._receive_text(receipt['payload']['text'],receipt['payload'].get('marks'))}
            elif receipt['kind']=='confirm':
                result={'resolved':s.resolve(**receipt['payload'])}
            else:raise ValueError('Неизвестный вид команды')
            with persistence.atomic(s):receipt['result']=result
            if not future.done():future.set_result(result)
        except guard.InputRejected as exc:
            try:
                with persistence.atomic(s):
                    receipt['status']='rejected'
                    receipt['result']={'error':{'kind':'invalid_command','message':str(exc)}}
            except db.PersistenceError as failure:
                exc=failure
                failed.add(receipt['id'])
            s.command_futures.pop(receipt['id'],None)
            if not future.done():future.set_exception(exc)
        except Exception as exc:
            failed.add(receipt['id'])
            s.command_futures.pop(receipt['id'],None)
            if not future.done():future.set_exception(exc)
        finally:
            s.command_futures.pop(receipt['id'],None)
            s._command=None
            s.command_queue.task_done()
        if s.durable and s.command_queue.empty():
            remaining=[r for r in db.required(repo.queued_commands,s.id) if r['id'] not in failed]
            for row in remaining[:s.command_queue.maxsize]:enqueue(s,row)


def resume(s):
    if s.durable:
        for receipt in db.required(repo.queued_commands,s.id)[:s.command_queue.maxsize]:enqueue(s,receipt)
    start_questions(s)


def start_questions(s):
    if s.qa_pending and (s.qa_task is None or s.qa_task.done()):
        s.qa_task=asyncio.create_task(answer_questions(s))


async def answer_questions(s):
    from . import qa
    try:
        while s.qa_pending:
            item=s.qa_pending[0]
            # Вопрос не теряется, даже если следующий уже принят.
            reply=await qa.reply(s,item['text'])
            with persistence.atomic(s):
                s.qa_pending=[q for q in s.qa_pending if q['id']!=item['id']]
                s.push('message',role='agent',text=reply,in_reply_to=item['id'])
    except db.PersistenceError:
        # Ответ ещё не зафиксирован. Останавливаем и основной ход, чтобы
        # ошибка фонового FAQ не оставила продолжающееся действие без хранения.
        if s.task is not None and not s.task.done():s.task.cancel()
        s.signal('error',error_kind='persistence',text='Не удалось сохранить ответ. Вопрос сохранён; продолжите диалог после восстановления соединения.')
