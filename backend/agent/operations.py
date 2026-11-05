"""Журнал инструментов и безопасная граница асинхронной работы."""
from copy import deepcopy
import uuid

from .. import db


async def run(s,call,execute):
    operation_id=call.id or uuid.uuid4().hex
    request_seq=s.request_inputs[-1]['seq'] if s.request_inputs else s.request_review_seq
    previous=s.operations.get(operation_id)
    if previous and previous.get('status')=='completed' and previous.get('request_seq')==request_seq:
        if previous['name']!=call.name or previous['arguments']!=call.arguments:
            return {'error_kind':'operation_conflict','error':'ID вызова уже использован с другими аргументами.'}
        return deepcopy(previous['result'])
    active=s.active_operation
    if active and active.get('id')==operation_id and (active['name']!=call.name or active['arguments']!=call.arguments):
        return {'error_kind':'operation_conflict','error':'ID активного вызова уже использован с другими аргументами.'}
    if not active or active.get('id')!=operation_id:
        active={'id':operation_id,'name':call.name,'arguments':deepcopy(call.arguments),'request_seq':request_seq,
                'status':'running','profile_revision':s.profile_revision,'selection_revision':s.selection_revision}
        s.active_operation=active
    s.remember()
    try:
        result=await execute(s,call)
    except db.PersistenceError:
        raise
    except BaseException as exc:
        # При отмене процесса оставляем долговечный checkpoint для восстановления.
        from .session import Superseded
        if isinstance(exc,Superseded):
            s.operations[operation_id]={**active,'status':'superseded'}
            s.active_operation=None
            s.remember()
        raise
    completed=s.operations.get(operation_id) or {}
    if completed.get('status')=='completed' and completed.get('request_seq')==request_seq:return result
    s.operations[operation_id]={**active,'status':'completed','result':deepcopy(result)}
    s.active_operation=None
    s.pending_decisions={k:t for k,t in s.pending_decisions.items() if t.get('status')!='consumed'}
    # Незавершённая операция хранится отдельно; завершённые записи ограничены снимком.
    if len(s.operations)>100:s.operations=dict(list(s.operations.items())[-100:])
    s.remember()
    return result
