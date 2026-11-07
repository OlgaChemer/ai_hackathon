"""Журнал задания фиксируется до публикации клиенту. Повтор команды не запускает модель повторно."""
from copy import deepcopy
import hashlib
import json
import uuid
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select,func
from ..db.models import WorkshopJob,utcnow


def required(fn,*args,**kwargs):
    from .. import db
    try:
        with db.session() as connection:return fn(connection,*args,**kwargs)
    except HTTPException:raise
    except db.PersistenceError:raise
    except Exception as exc:
        raise db.PersistenceError('Не удалось сохранить задание. Повторите запрос с тем же ID.') from exc


def owned(connection,uid,job_id):
    row=connection.get(WorkshopJob,job_id)
    if row is None or row.user_id!=uid:raise HTTPException(404,'Задание недоступно.')
    return row


def public(row):
    return {k:deepcopy(getattr(row,k)) for k in ('id','chat_id','kind','prompt','status','events','result','error')}


def fingerprint(data):return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def create(connection,uid,payload,provider,snapshot):
    old=connection.scalar(select(WorkshopJob).where(WorkshopJob.user_id==uid,WorkshopJob.command_id==payload['command_id']))
    signature=fingerprint(payload)
    if old:
        if old.snapshot.get('command_fingerprint')!=signature:raise HTTPException(409,'ID команды уже использован для другого задания.')
        return public(old)
    active=connection.scalar(select(func.count()).select_from(WorkshopJob).where(WorkshopJob.status.in_(['queued','running'])))
    if active>=3:raise HTTPException(429,'Мастерская занята. Дождитесь завершения текущих задач.')
    daily=connection.scalar(select(func.count()).select_from(WorkshopJob).where(WorkshopJob.user_id==uid,WorkshopJob.created_at>=utcnow()-timedelta(days=1)))
    if daily>=30:raise HTTPException(429,'Достигнут дневной лимит: 30 задач мастерской.')
    row=WorkshopJob(id=uuid.uuid4().hex,user_id=uid,chat_id=snapshot['chat_id'],command_id=payload['command_id'],
        kind=payload['kind'],prompt=payload['prompt'],provider=provider,status='queued',
        snapshot={**snapshot,'command_fingerprint':signature},events=[{'text':'Задание принято. Готовлю данные.'}])
    connection.add(row);connection.flush();return public(row)


def progress(connection,job_id,text):
    row=connection.get(WorkshopJob,job_id)
    if row and row.status in ('queued','running'):
        row.events=[*row.events,{'text':str(text)[:250]}][-80:];row.updated_at=utcnow()


def recover(connection):
    for row in connection.scalars(select(WorkshopJob).where(WorkshopJob.status.in_(['queued','running']))):
        row.status='interrupted';row.error='Работа прервана перезапуском сервера. Данные сохранены; запустите новое задание.'
        row.updated_at=utcnow()
