"""Неполные запросы: настоящая модель, новые комбинации и полные цепочки.

--case semantic не читает площадки и не вызывает бизнес-действия.
--case dialogues работает на отдельном flexible_server --live-llm или публично
с настоящими объявлениями. Ни один сценарий не разрешает звонок.
"""
import argparse
import asyncio
import json
from pathlib import Path
from backend import db
from backend.agent import session,understanding as U
from backend.agent.profile import merge_profile
from backend.agent.actions.request import updated_profile
from backend.llm import get_llm
from backend.release import manifest
from tests.e2e.live_dialogue_cases import Dialogue
from tests.e2e.live_budget_and_revisions import next_step


async def semantic(provider=None,report=None):
    from backend.llm.factory import build_llm
    db.configure('sqlite://');llm=build_llm(provider) if provider else get_llm()
    cases=[
        ('rooms','В Москве бюджет 20 млн, на сколько комнат хватит?'),
        ('area','Какую площадь можно купить за мой бюджет?'),
        ('open','В Москве нужна квартира выше 10 этажа у парка. Точный район и бюджет неважны.'),
        ('remove','Убери ограничение бюджета и комнатности. Остальное сохрани.'),
        ('unknown','Комнатность пока не знаю, бюджет не определил. Подбери варианты в Москве.'),
        ('answer','Любой'),
    ]
    for kind,text in cases:
        s=session.create_session('',llm)
        if kind in ('area','remove','answer'):
            s.profile=merge_profile({'city':'Москва','rooms':2 if kind=='remove' else None,'budget_max':20_000_000,'not_first_floor':kind=='answer','not_last_floor':kind=='answer'})
        if kind=='answer':
            s.active_operation={'name':'ask_client','status':'waiting'}
            s.pending_decisions={'clarify':{'kind':'clarify','text':'Какой этаж для вас предпочтительнее?',
                'payload':{'chips':['Любой','Не первый','Не последний']},'status':'waiting'}}
        e=s.push('message',role='user',text=text);s.accept_request(text,e['seq'])
        r=await U.prepare(s);assert r['decision']!='clarify',(kind,r)
        p=updated_profile(s.profile,r['changes'])
        if kind in ('rooms','area'):
            assert r.get('requested_action')=='market_prices',(kind,r)
            q=r['research'];assert q['respect_budget'],r
            if kind=='rooms':assert q['group_by']=='rooms' and p['rooms'] is None and p['budget_max']==20_000_000,r
            else:assert any(a['field']=='area' for a in q['aggregates']),r
        elif kind=='open':
            assert p['city']=='Москва' and p['budget_max'] is None and p['rooms'] is None,r
            assert 'park' in p['poi'] and any(f['field']=='floor' and f['op']=='gt' and f['values']==['10'] for f in p['filters']),r
            assert p['field_states'].get('budget_max')=='unrestricted',r
        elif kind=='remove':assert p['budget_max'] is None and p['rooms'] is None and p['city']=='Москва',r
        elif kind=='unknown':
            assert p['budget_max'] is None and p['rooms'] is None and p['city']=='Москва',r
            assert p['field_states'].get('rooms')=='unknown' and p['field_states'].get('budget_max')=='unknown',r
        else:assert not p['not_first_floor'] and not p['not_last_floor'] and p['budget_max']==20_000_000,r
        print(json.dumps({'semantic':kind,'ok':True},ensure_ascii=False),flush=True)
    if report:Path(report).write_text(json.dumps({'manifest':manifest(llm),'passed':len(cases),'total':len(cases),
        'case_names':[c[0] for c in cases],'scope':'NLU and bound card choices; no business actions or telephone calls'},ensure_ascii=False,indent=2))


async def dialogues(base):
    d=Dialogue(base)
    try:
        await d.start('Хочу красивую квартиру у парка в Москве.')
        e=await next_step(d);assert e.get('kind')=='selection' and d.cards,e
        assert d.profile['rooms'] is None and d.profile['budget_max'] is None,d.profile
        await d.send('Бюджет 20 млн, на сколько комнат хватит?');e=await next_step(d)
        m=d.profile.get('market');assert m and m['scope']['budget_applied'],(e,d.messages[-3:])
        assert m['analysis']['groups'] and m['scope']['research']['group_by']=='rooms',m
        assert d.profile['rooms'] is None and d.profile['budget_max']==20_000_000,d.profile
        await d.send('Покажи три самых просторных предложения в этом бюджете.');e=await next_step(d)
        assert e.get('kind')=='selection' and len(d.cards)==3,(e,d.messages[-3:])
        assert all(x['price']<=20_000_000 for x in d.cards)
        print(json.dumps({'dialogue':'unknown_rooms_to_budget_research_to_selection','ok':True,'cards':len(d.cards)}),flush=True)
    finally:await d.close()
    d=Dialogue(base)
    try:
        await d.start('В Москве хочу квартиру выше 10 этажа у парка. Точный район, комнатность и бюджет неважны.')
        e=await next_step(d);assert e.get('kind')=='selection' and d.cards,(e,d.messages[-3:])
        assert d.profile['budget_max'] is None and d.profile['rooms'] is None,d.profile
        assert all(x['floor']>10 for x in d.cards),d.cards
        await d.send('Теперь только две комнаты до 12 миллионов, прочие пожелания сохрани.');e=await next_step(d)
        assert e.get('kind')=='selection' and d.cards,(e,d.messages[-3:])
        assert d.profile['rooms_options']==[2] and d.profile['budget_max']==12_000_000,d.profile
        await d.send('Убери ограничение бюджета и комнатности. Этаж и парк сохрани.');e=await next_step(d)
        assert e.get('kind')=='selection' and d.cards,(e,d.messages[-3:])
        assert d.profile['budget_max'] is None and d.profile['rooms'] is None,d.profile
        assert all(x['floor']>10 for x in d.cards)
        print(json.dumps({'dialogue':'open_floor_park_to_constraints_to_unrestricted','ok':True,'cards':len(d.cards)}),flush=True)
    finally:await d.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8797');p.add_argument('--case',choices=['semantic','dialogues','all'],default='all')
    p.add_argument('--provider',choices=['gigachat','deepseek']);p.add_argument('--report')
    a=p.parse_args()
    async def main():
        if a.case in ('semantic','all'):await semantic(a.provider,a.report)
        if a.case in ('dialogues','all'):await dialogues(a.base)
    asyncio.run(main())
