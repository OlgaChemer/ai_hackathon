"""Связанный диалог с реальным Ultra на изолированном flexible_server --live-llm.

Без настоящих звонков: останавливается на предложении программы разговора.
"""
import argparse
import asyncio
import json

from tests.e2e.live_dialogue_cases import Dialogue


async def main(base):
    d=Dialogue(base)
    report=[]

    async def check(label,text=None,number=None):
        if text:
            await d.send(text)
        outcome=await d.round()
        assert outcome['outcome']=='selection',(label,outcome)
        if number is not None:
            assert len(d.cards)==number,(label,len(d.cards))
        assert d.profile['city']=='Москва',(label,d.profile)
        assert d.profile['budget_max']==30_000_000,(label,d.profile)
        assert d.profile.get('center_required'),(label,'lost mandatory center')
        report.append({'case':label,'cards':len(d.cards),'city':d.profile['city'],'filters':d.profile.get('filters'),
                       'ids':[x['id'] for x in d.cards]})
        print(json.dumps(report[-1],ensure_ascii=False),flush=True)

    try:
        health=(await d.client.get('/api/health')).json()
        assert health['provider']=='gigachat',health
        await d.start('Ищу двухкомнатную квартиру в центре Москвы до 30 млн рублей, в новостройке. Покупаю за свои. Рядом нужен детский сад.')
        await check('initial',number=10)
        liked=[x['id'] for x in d.cards]
        d.cards=[]
        response=await d.client.post(f'/api/session/{d.sid}/confirm',json={
            'kind':'selection','approved':True,'data':{'action':'more','liked':liked,'disliked':[]}})
        response.raise_for_status()
        await check('more_with_ten_likes',number=10)
        assert not set(liked)&{x['id'] for x in d.cards}
        await check('top_three','Из всех найденных вариантов оставь три наиболее подходящих по моим параметрам.',3)
        await check('exact_delivery_and_sort','Оставь только те, что сдаются именно в 2027 году. Покажи три самых просторных из всех найденных.',3)
        assert all(x.get('delivery_year')==2027 for x in d.cards)
        areas=[x['area'] for x in d.cards]
        assert areas==sorted(areas,reverse=True),areas
        assert all(not any(c['key']=='year_min' for c in x['match']['checks']) for x in d.cards)
        start=len(d.timeline)
        await check('school_refinement','Ещё нужна школа рядом, до 1 км пешком. Остальные условия сохрани.',3)
        assert 'school' in d.profile['poi']
        assert all(x.get('delivery_year')==2027 for x in d.cards)
        assert not any(e.get('kind')=='call' and e['type']=='confirm' for e in d.timeline[start:])
        start=len(d.timeline)
        await d.send('Уточни у продавца шумоизоляцию и были ли перепланировки. Пока не звони.')
        outcome=await d.round()
        assert outcome['outcome'] in ('selection','text_wait'),outcome
        assert not any(e.get('kind')=='call' and e['type']=='confirm' for e in d.timeline[start:])
        report.append({'case':'seller_questions_without_call','outcome':outcome['outcome']})
        print(json.dumps(report[-1],ensure_ascii=False),flush=True)
        # При text_wait карточки остаются, новые ID берём из последней выдачи.
        await d.send('Позвони по второй квартире. Продавца сыграет ИИ, реального звонка не нужно.')
        outcome=await d.round()
        assert outcome.get('kind')=='call' and outcome['outcome']=='unexpected_confirmation',outcome
        report.append({'case':'explicit_call_card_only','outcome':'call_confirmation'})
        print(json.dumps(report[-1],ensure_ascii=False),flush=True)
    finally:
        await d.close()
    print(json.dumps({'passed':len(report),'checks':report},ensure_ascii=False),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--base',default='http://127.0.0.1:8797')
    asyncio.run(main(ap.parse_args().base))
