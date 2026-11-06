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
    from backend.agent.pricing import CITY_CENTERS
    from tests.e2e.policy_llm import PolicyLLM
    from unittest.mock import AsyncMock

    if not live_llm:
        main.get_llm = lambda: PolicyLLM()

    async def collect(profile,on_batch=None):
        rows=[]
        reports=[]
        cities=[profile['city'],*(profile.get('cities') or [])]
        for city in cities:
            lat,lng=CITY_CENTERS.get(city,(55.7986,49.1053))
            for rooms in profile.get('rooms_options') or ([profile['rooms']] if profile.get('rooms') is not None else [0,1,2,3,4]):
                for new in sources.markets(profile):
                    for i in range(40):
                        rows.append(listing(id=f'cian-{city}-{rooms}-{int(new)}-{i}',city=city,rooms=rooms,
                            address=f'Тестовая улица, {i+1}',price=int(min(7_000_000,(profile.get('budget_max') or 30_000_000)*.7))+i*10000,area=40+i,
                            floor=1+i%20,
                            lat=lat+(i%4)*.003,lng=lng+(i%4)*.003,is_new=new,
                            year=None if new else 2015,deadline=f'4 кв. {2026+i%4}' if new else None,
                            delivery_year=2026+i%4 if new else None,delivery_quarter=4 if new else None,
                            photo=None,description='Синтетическое объявление из тестового стенда'))
                    reports.append({'source':'Изолированный тестовый пул','status':'live','count':40,
                                    'city':city,'rooms':rooms,'new':new})
                    if on_batch:on_batch(rows[-40:])
        return rows,reports

    async def nearby(rows,kinds,**kwargs):
        for x in rows:
            for kind in kinds:
                x.setdefault('nearby',{})[kind]={'name':'Тестовый объект','dist':350,'lat':x['lat']+.001,'lng':x['lng']}
                x.setdefault('nearby_evidence',{})[kind]={'source':'fixture','distance_kind':'straight_line'}
        return True

    async def market_search(city,rooms,budget,page=1,okrug=None,new=False):
        lat,lng=CITY_CENTERS.get(city,(55.7986,49.1053))
        rows=[listing(id=f'market-fixture-{city}-{rooms}-{new}-{page}-{i}',city=city,rooms=rooms if rooms is not None else 1+i%4,is_new=new,
                      price=(20_000_000+int(new)*10_000_000+i*200_000 if rooms is not None else 8_000_000+(1+i%4)*2_000_000+int(new)*2_000_000+i*100_000),area=60+i,
                      lat=lat+.001*(i%4),lng=lng+.001*(i%4),photo=None) for i in range(20)]
        return rows,{'status':'live','source':'Изолированный тестовый пул','fetched_at':'2026-11-05T00:00:00+00:00'}

    sources.collect=collect
    sources.cian.search=market_search
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
    os.environ['COOKIE_SECURE']='false'  # только изолированный HTTP-стенд на loopback
    patch(args.live_llm)
    import uvicorn
    from backend.main import app
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
