"""Настоящая модель: неподдержанное пожелание, продолжение и два быстрых FAQ.

Запускать на flexible_server --live-llm с отдельной БД. Телефоны не набирает.
"""
import argparse
import asyncio
import json
from tests.e2e.live_dialogue_cases import Dialogue, _timeout


async def main(base):
    d=Dialogue(base)
    try:
        await d.start('Казань, двухкомнатная до 9 миллионов, вторичка, за свои средства.')
        outcome=await d.round()
        assert outcome['outcome']=='selection' and d.cards,outcome
        profile={k:d.profile.get(k) for k in ('city','rooms_options','budget_max','payment','housing')}
        await d.send('Хочу квартиру где внутри бассейн')
        outcome=await d.round()
        assert outcome['outcome']=='clarify',outcome
        assert 'бассейн' in outcome['text'].casefold(),outcome
        assert not any(f['field']=='nearby:fitness' for f in d.profile.get('filters') or []),d.profile
        await d.send('Тогда покажи три самые просторные квартиры, бассейн можно не проверять.')
        outcome=await d.round()
        assert outcome['outcome']=='selection' and len(d.cards)==3,outcome
        assert {k:d.profile.get(k) for k in profile}==profile,d.profile
        print(json.dumps({'case':'unsupported_wish_and_continuation','ok':True},ensure_ascii=False),flush=True)
        requests=[('p0-q1','Что означает цена по аналогам?'),('p0-q2','Что означает непроверенное пожелание?')]
        results=await asyncio.gather(*(d.client.post(f'/api/chat/{d.sid}/message',
            json={'text':text,'command_id':identifier}) for identifier,text in requests))
        for response in results:
            response.raise_for_status()
            assert response.json()['handled_as']=='question',response.json()
        answers={}
        async with _timeout(90):
            while len(answers)<2:
                event=await d.events.get()
                assert event['type'] not in ('error','transport_error'),event
                identifier=event.get('in_reply_to')
                if identifier in dict(requests):
                    assert identifier not in answers,'Повторный ответ на одну команду'
                    assert event.get('text','').strip(),event
                    answers[identifier]=event['text']
        identifier,text=requests[0]
        repeat=await d.client.post(f'/api/chat/{d.sid}/message',json={'text':text,'command_id':identifier})
        repeat.raise_for_status();assert repeat.json()['handled_as']=='question',repeat.json()
        print(json.dumps({'case':'two_questions_and_duplicate_receipt','ok':True,'answers':len(answers)},ensure_ascii=False),flush=True)
    finally:await d.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8797')
    asyncio.run(main(p.parse_args().base))
