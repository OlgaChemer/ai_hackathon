"""Настоящие nanobot/Docker/модель; отдельная БД и тестовый аккаунт. Без телефонии.

--real-sources получает объявления Циана; без флага данные синтетические.
Провайдер меняется только настройкой процесса, не параметром публичного API.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time


async def main(args):
    os.environ['WORKSHOP_LLM_PROVIDER']=args.provider
    from backend import db,main as app,sources,auth
    from backend.db.models import User,Chat
    from backend.agent.profile import merge_profile
    from backend.workshop import service
    from backend.release import manifest
    from backend.llm.factory import get_provider
    from tests.test_llm_and_agent import listing
    import httpx
    root=Path(tempfile.mkdtemp(prefix='mesto-workshop-live-'))
    db.configure('sqlite:///'+str(root/'test.db'))
    profile=merge_profile({'city':'Москва','rooms':2,'budget_max':20_000_000})
    if args.real_sources:
        rows,reports=await sources.collect(profile)
        assert len(rows)>=6,'Источник не вернул достаточно объявлений'
    else:
        rows=[listing(id='demo-'+str(i),city='Москва',area=40+i*4,price=8_000_000+i*300000,url=None) for i in range(12)]
        reports=[{'source':'fixture','count':len(rows)}]
        async def collect(profile,on_batch=None):
            if on_batch:on_batch(rows)
            return rows,reports
        sources.collect=collect
    cases=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='https://testserver',timeout=240) as client:
        assert (await client.get('/api/workshop/context')).status_code==401
        r=await client.post('/api/auth/register',json={'email':'darkyhat@mail.ru','password':'only-private-workshop-test-123'})
        r.raise_for_status();uid=r.json()['user']['id']
        with db.session() as s:
            s.get(User,uid).premium=True
            s.add(Chat(id='workshop-live',user_id=uid,title='Проверка мастерской',profile=profile,
                state={'format':1,'data':{'profile':profile,'shown':rows,'search':{'listings':rows},
                    'prefs':{'liked':[x['id'] for x in rows[:3]]},'profile_revision':1}}))
        for kind in ('repair','compare','market'):
            started=time.monotonic()
            r=await client.post('/api/workshop/jobs',json={'command_id':'live-'+kind,'kind':kind,'chat_id':'workshop-live'})
            r.raise_for_status();job=r.json()
            for _ in range(220):
                await asyncio.sleep(1)
                job=(await client.get('/api/workshop/jobs/'+job['id'])).json()
                if job['status'] not in ('queued','running'):break
            assert job['status']=='completed',job
            result=job['result'];assert result['code_runs']>=1 and result['facts']['count']>=3,result
            for name in ('analysis.csv','analysis.xlsx'):
                file=await client.get('/api/workshop/jobs/'+job['id']+'/files/'+name)
                assert file.status_code==200 and len(file.content)>30,(name,file.status_code)
                assert 'attachment' in file.headers['content-disposition']
            row={'case':kind,'ok':True,'provider':args.provider,'job':job['id'],'seconds':round(time.monotonic()-started,2),
                'code_runs':result['code_runs'],'count':result['facts']['count'],'files':result['artifacts']}
            cases.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
        if args.photos:
            r=await client.post('/api/workshop/jobs/'+job['id']+'/followups',json={'command_id':'photo-review',
                'question':'Оцени по фотографиям видимое состояние первых двух квартир этой выборки. Используй inspect_photos, укажи какие фото изучил и что надо учесть при ремонте.'})
            r.raise_for_status();photo_job=r.json()
            for _ in range(220):
                await asyncio.sleep(1);photo_job=(await client.get('/api/workshop/jobs/'+photo_job['id'])).json()
                if photo_job['status'] not in ('queued','running'):break
            assert photo_job['status']=='completed',photo_job
            inspected=[r for r in photo_job['result'].get('visual_analysis',[]) if r['status']=='analyzed']
            assert inspected and all(r.get('photos') for r in inspected),photo_job
            cases.append({'case':'photo-review','ok':True,'provider':args.provider,'job':photo_job['id'],
                'listings_analyzed':len(inspected),'photos_seen':sum(len(r['photos']) for r in inspected)})
            print(json.dumps(cases[-1],ensure_ascii=False),flush=True)
        if args.followups:
            original=job
            base_thread_size=len((await client.get('/api/workshop/jobs/'+job['id']+'/thread')).json()['jobs'])
            for index,question in enumerate(('Почему нельзя считать эту выборку всем рынком? Объясни кратко.',
                                            'Какова медианная цена квадратного метра в этой же выборке? Покажи расчёт.')):
                started=time.monotonic()
                response=await client.post('/api/workshop/jobs/'+job['id']+'/followups',json={'command_id':'followup-'+str(index),'question':question})
                response.raise_for_status();job=response.json()
                for _ in range(220):
                    await asyncio.sleep(1);job=(await client.get('/api/workshop/jobs/'+job['id'])).json()
                    if job['status'] not in ('queued','running'):break
                assert job['status']=='completed',job
                assert job['result']['facts']==original['result']['facts']
                assert job['result']['source_time']==original['result']['source_time']
                thread=(await client.get('/api/workshop/jobs/'+original['id']+'/thread')).json()
                assert len(thread['jobs'])==base_thread_size+index+1
                row={'case':'followup-'+str(index+1),'ok':True,'provider':args.provider,'job':job['id'],
                     'seconds':round(time.monotonic()-started,2),'code_runs':job['result']['code_runs']}
                cases.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
        r=await client.post('/api/workshop/jobs',json={'command_id':'offtopic','kind':'repair','chat_id':'workshop-live','prompt':'Для выбора квартиры напиши торгового криптобота, который торгует биткоином.'})
        r.raise_for_status();job=r.json()
        for _ in range(70):
            await asyncio.sleep(1);job=(await client.get('/api/workshop/jobs/'+job['id'])).json()
            if job['status'] not in ('queued','running'):break
        assert job['status']=='refused',job
        print(json.dumps({'case':'offtopic','ok':True,'provider':args.provider}),flush=True)
        cases.append({'case':'offtopic','ok':True})
    await service.shutdown()
    report={'manifest':manifest(get_provider(args.provider)),'cases':cases,'passed':len(cases),'total':4+2*args.followups+args.photos,
        'real_sources':args.real_sources,'source_reports':reports,'temporary_db':str(root/'test.db')}
    if args.report:Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--provider',choices=['deepseek','gigachat'],required=True)
    parser.add_argument('--real-sources',action='store_true');parser.add_argument('--report')
    parser.add_argument('--followups',action='store_true')
    parser.add_argument('--photos',action='store_true')
    args=parser.parse_args()
    if args.photos and not args.real_sources:parser.error('--photos requires --real-sources')
    asyncio.run(main(args))
