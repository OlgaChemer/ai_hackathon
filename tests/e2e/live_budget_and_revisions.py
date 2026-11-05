"""Настоящая модель: помощь с бюджетом и смена условий после поздних решений.

С --case budget можно проверять публичный сайт. Поздние решения проверять на
изолированном flexible_server --live-llm: ИИ-продавец, без телефонного набора.
"""
import argparse
import asyncio
import json
from tests.e2e.live_dialogue_cases import Dialogue,_timeout


async def next_step(d,auto=True):
    async with _timeout(150):
        while True:
            e=await d.events.get();kind=e.get('kind')
            assert e['type'] not in ('error','transport_error'),e
            if e['type']=='result':
                if kind=='profile':d.profile=e['data']
                if kind=='selection':d.cards=e['data']['cards']
            if e['type']=='message' and e.get('role')=='agent':
                d.messages.append(e['text'])
                if e.get('await_reply'):return e
            if e['type']=='done':return e
            if e['type']!='confirm':continue
            if auto and kind in ('red_lines','payment','housing'):
                await approve(d,e,{'payment':'cash'} if kind=='payment' else {'housing':'both'} if kind=='housing' else {})
                continue
            return e


async def approve(d,e,data=None):
    r=await d.client.post(f'/api/session/{d.sid}/confirm',json={'kind':e['kind'],'approved':True,
        'decision_id':e['decision_id'],'data':data or {}})
    r.raise_for_status();assert r.json()['ok'],r.json()


async def budget(base):
    d=Dialogue(base)
    try:
        await d.start('Найди мне квартиру в центре Москвы по выгодной цене.')
        step=await next_step(d)
        assert step.get('kind') in ('clarify','finance') or (step['type']=='message' and step.get('await_reply')),step
        await d.send('3');step=await next_step(d)
        assert step.get('kind') in ('clarify','finance') or (step['type']=='message' and step.get('await_reply')),step
        await d.send('Не знаю, какой средний?');step=await next_step(d)
        m=d.profile.get('market');assert m and m.get('median'),(step,d.messages)
        assert d.profile['budget_max'] is None
        assert m['scope']['rooms']==[3] and m['scope']['center_radius_km']==5
        assert set(m['scope']['markets'])=={True,False}
        assert any('медиана' in text.casefold() and 'выборк' in text.casefold() for text in d.messages),d.messages
        await d.send('Все равно новостройка или вторичка, ближе к центру. За наличку.')
        step=await next_step(d)
        assert d.profile['housing']=='both' and d.profile['payment']=='cash',d.profile
        await d.send('Да я хз, какой средний?');step=await next_step(d)
        assert d.profile.get('market',{}).get('median')==m['median']
        assert d.profile['budget_max'] is None
        await d.send('Тогда лимит 80 миллионов рублей. Остальные условия сохрани. Начинай подбор.')
        step=await next_step(d)
        assert step.get('kind')=='selection' and d.cards,(step,d.messages[-4:])
        assert d.profile['rooms_options']==[3] and d.profile['budget_max']==80_000_000
        assert d.profile['city']=='Москва' and d.profile['center_required']
        print(json.dumps({'case':'unknown_budget_to_market_prices_to_selection','ok':True,
            'median':m['median'],'sample':m['count'],'markets':m['markets'],'cards':len(d.cards)},ensure_ascii=False),flush=True)
    finally:await d.close()


async def revision(base,phase):
    d=Dialogue(base)
    try:
        await d.start('Казань, две комнаты до 9 миллионов, вторичка, покупаю за наличные.')
        step=await next_step(d);assert step.get('kind')=='selection' and d.cards,step
        old=d.cards[0]['id']
        await d.send('Позвони продавцу квартиры номер 1 из текущей подборки. Продавца играет ИИ.')
        step=await next_step(d);assert step.get('kind')=='call',step
        await approve(d,step,{'mode':'auto'})
        step=await next_step(d);assert step.get('kind')=='price',step
        if phase!='price':
            await approve(d,step)
            step=await next_step(d);assert step.get('kind')=='object',step
            await approve(d,step)
            step=await next_step(d);assert step.get('kind')=='sign',step
            if phase=='completed':
                await approve(d,step)
                step=await next_step(d);assert step['type']=='done',step
        before=len(d.timeline)
        await d.send('Передумал. Теперь ищи в Самаре, одну комнату до 8 миллионов рублей, вторичку, за наличные. Это новый подбор вместо прежней квартиры.')
        step=await next_step(d)
        assert step.get('kind')=='selection' and d.cards,(phase,step,d.messages[-3:])
        assert d.profile['city']=='Самара' and d.profile['rooms_options']==[1] and d.profile['budget_max']==8_000_000,d.profile
        assert all(x['id']!=old and x['city']=='Самара' and x['rooms']==1 for x in d.cards)
        assert any(e['type']=='purchase_reset' for e in d.timeline[before:]),phase
        print(json.dumps({'case':'new_request_after_'+phase,'ok':True,'cards':len(d.cards)},ensure_ascii=False),flush=True)
    finally:await d.close()


async def agreements():
    from backend import db
    from backend.agent import session,understanding
    from backend.agent.profile import merge_profile
    from backend.llm import get_llm
    db.configure('sqlite://');llm=get_llm()
    for text,action in [('Да','market_prices'),('Давай','market_prices'),('Покажи','market_prices'),
                        ('Какую сумму обычно просят за такие квартиры?','market_prices'),
                        ('Рассчитай личный доступный бюджет по моим доходам и накоплениям.','affordable_budget')]:
        s=session.create_session('',llm)
        s.profile=merge_profile({'city':'Москва','rooms':3,'housing':'both','payment':'cash','center_required':True})
        s.push('message',role='user',text='Не знаю, какой средний бюджет.')
        s.push('message',role='agent',text='Могу показать рыночные цены на трёхкомнатные квартиры в центре Москвы. Хотите?')
        restored=session.restore_session(s.id,'',llm)
        await restored.receive_text(text,command_id='fresh-input')
        result=restored.request_review
        assert result and result.get('requested_action')==action,(text,result)
        assert not result['changes'] and restored.profile['budget_max'] is None,(text,result)
        print(json.dumps({'case':'agreement_after_restart','input':text,'action':action,'ok':True},ensure_ascii=False),flush=True)


async def main(args):
    if args.case in ('all','budget'):await budget(args.base)
    if args.case in ('all','revisions'):
        for phase in ('price','chosen','completed'):await revision(args.base,phase)
    if args.case in ('all','agreements'):await agreements()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--case',choices=['all','budget','revisions','agreements'],default='all')
    asyncio.run(main(p.parse_args()))
