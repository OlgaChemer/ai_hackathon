from pathlib import Path
from typing import Literal

from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field,StrictBool
from sqlalchemy import select
from .. import db
from ..config import settings
from ..db.models import WorkshopJob,utcnow
from . import data,service,store

router=APIRouter(prefix='/api/workshop')


class JobRequest(BaseModel):
    model_config={'extra':'forbid'}
    command_id:str=Field(min_length=1,max_length=64,pattern=r'^[a-zA-Z0-9_-]+$')
    chat_id:str=Field(min_length=1,max_length=32)
    kind:Literal['repair','compare','market']
    prompt:str=Field(default='',max_length=1000)
    liked:list[str]|None=Field(default=None,max_length=20)
    rate:float=Field(default=30000,ge=0,le=300000)
    reserve:float=Field(default=15,ge=0,le=100)
    whole_city:StrictBool=False


@router.get('/context')
def context(request:Request,chat_id:str|None=None):
    uid=service.premium(request)
    return store.required(data.context,uid,chat_id)


@router.get('/jobs')
def jobs(request:Request):
    uid=service.premium(request)
    return store.required(lambda s:{'jobs':[store.public(r) for r in s.scalars(select(WorkshopJob).where(WorkshopJob.user_id==uid).order_by(WorkshopJob.created_at.desc()).limit(20))]})


@router.post('/jobs',status_code=202)
async def create_job(body:JobRequest,request:Request):
    uid=service.premium(request)
    payload=body.model_dump();payload['prompt']=payload['prompt'].strip()
    result=store.required(service.create,uid,payload)
    if result['status']=='queued':service.launch(result['id'])
    return result


@router.get('/jobs/{job_id}')
def job(job_id:str,request:Request):
    uid=service.premium(request)
    return store.required(lambda s:store.public(store.owned(s,uid,job_id)))


@router.post('/jobs/{job_id}/cancel')
async def cancel(job_id:str,request:Request):
    uid=service.premium(request)
    def apply(s):
        r=store.owned(s,uid,job_id)
        if r.status in ('queued','running'):
            r.status='cancelled';r.error='Задание отменено вами.';r.updated_at=utcnow()
        return store.public(r)
    result=store.required(apply)
    if result['status']=='cancelled' and job_id in service.TASKS:service.TASKS[job_id].cancel()
    return result


@router.get('/jobs/{job_id}/files/{name}')
def artifact(job_id:str,name:str,request:Request):
    uid=service.premium(request)
    row=store.required(lambda s:store.public(store.owned(s,uid,job_id)))
    if row['status']!='completed' or name not in (row.get('result') or {}).get('artifacts',[]):raise HTTPException(404,'Файл недоступен.')
    if name not in ('analysis.xlsx','analysis.csv','chart.svg','report.html'):raise HTTPException(404,'Файл недоступен.')
    path=Path(settings.workshop_root)/job_id/'artifacts'/name
    if path.is_symlink() or not path.is_file():raise HTTPException(404,'Файл недоступен.')
    return FileResponse(path,filename=name,media_type='application/octet-stream',headers={'Cache-Control':'no-store'})
