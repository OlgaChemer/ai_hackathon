"""Изолированный стенд: настоящий агент, синтетический пул, опционально настоящий Ultra.

python -m tests.e2e.flexible_server --port 8796 [--live-llm]
Не использует рабочую БД, внешнюю карту, телефонию и живой Циан.
"""
import argparse
import os
import tempfile


def patch(live_llm=False):
    from backend import main, sources, geo
    from backend.sources import places
    from tests.test_llm_and_agent import listing
    from tests.e2e.policy_llm import PolicyLLM
    from unittest.mock import AsyncMock

    if not live_llm:
        main.get_llm = lambda: PolicyLLM()

    async def collect(profile):
        rows=[]
        cities=[profile['city'],*(profile.get('cities') or [])]
        for city in cities:
            lat,lng=(55.7539,37.6208) if city=='Москва' else (55.7986,49.1053)
            for rooms in profile.get('rooms_options') or [profile['rooms']]:
                for i in range(40):
                    rows.append(listing(id=f'cian-{city}-{rooms}-{i}',city=city,rooms=rooms,
                        address=f'Тестовая улица, {i+1}',price=7_000_000+i*10000,area=40+i,
                        lat=lat+(i%4)*.003,lng=lng+(i%4)*.003,is_new=profile.get('housing')!='secondary',
                        year=None,deadline=f'4 кв. {2026+i%4}',delivery_year=2026+i%4,delivery_quarter=4,
                        photo=None,description='Синтетическое объявление из тестового стенда'))
        return rows,[{'source':'Изолированный тестовый пул','status':'live','count':len(rows)}]

    async def nearby(rows,kinds,**kwargs):
        for x in rows:
            for kind in kinds:
                x.setdefault('nearby',{})[kind]={'name':'Тестовый объект','dist':350,'lat':x['lat']+.001,'lng':x['lng']}
                x.setdefault('nearby_evidence',{})[kind]={'source':'fixture','distance_kind':'straight_line'}
        return True

    sources.collect=collect
    sources.more=AsyncMock(return_value=[])
    sources.stored=lambda *a,**k:[]
    sources.comparables=lambda *a,**k:[]
    sources.verify=AsyncMock(return_value={'alive':True,'listing':None,'note':'fixture','checked_at':None})
    geo.nearby=nearby
    main.phone.configured=lambda:False
    main.stt.preload_in_background=lambda:None
    main.maptiles.configuration=lambda:{'provider':'osm'}
    places.status=lambda:{'configured':False,'provider':'fixture'}


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--port',type=int,default=8796)
    ap.add_argument('--live-llm',action='store_true')
    args=ap.parse_args()
    os.environ['DATABASE_URL']=f"sqlite:///{tempfile.mkdtemp(prefix='flexible-agent-')}/app.db"
    os.environ['REFRESH_AT']=''
    patch(args.live_llm)
    import uvicorn
    from backend.main import app
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
