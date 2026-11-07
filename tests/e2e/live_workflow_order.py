"""Настоящая модель: первоначальный лимит/оплата, уточнение пожеланий, новый лимит, переход к звонку.

Расходует квоту, создаёт тестовый диалог. Останавливается на карточке звонка без набора телефона.
"""
import argparse
import asyncio
import json

from tests.e2e.live_dialogue_cases import Dialogue,_timeout


async def main(base):
    d=Dialogue(base)
    try:
        await d.start('Самара, однушка или двушка до 10 млн, вторичка, ремонт любой.')
        first=await d.round()
        assert first['outcome']=='selection',first
        assert first['automated'].count('red_lines')==1 and first['automated'].count('payment')==1,first
        await d.send('Парк желателен, но не обязателен. Остальное оставь прежним.')
        refined=await d.round()
        assert refined['outcome']=='selection' and 'red_lines' not in refined['automated'] and 'payment' not in refined['automated'],refined
        await d.send('Теперь лимит 12 миллионов. Остальные условия оставь прежними.')
        changed=await d.round()
        assert changed['outcome']=='selection' and changed['automated'].count('red_lines')==1 and 'payment' not in changed['automated'],changed
        assert d.profile['budget_max']==12_000_000
        response=await d.client.post(f'/api/session/{d.sid}/confirm',json={'kind':'selection','approved':True,
            'data':{'action':'proceed','liked':[d.cards[0]['id']],'disliked':[]}})
        response.raise_for_status()
        call=None
        async with _timeout(80):
            while not call:
                event=await d.events.get()
                if event['type']=='confirm':
                    assert event.get('kind') not in ('red_lines','payment'),event
                    assert event.get('kind')=='call',event
                    call=event
                if event['type']=='message' and event.get('await_reply'):
                    await d.send('Да, позвоните по отмеченной квартире.')
                assert event['type']!='error',event
        kinds=[e.get('kind') for e in d.timeline if e['type']=='confirm']
        assert kinds.count('red_lines')==2 and kinds.count('payment')==1,kinds
        print(json.dumps({'ok':True,'session':d.sid,'confirmations':kinds,'new_budget':d.profile['budget_max'],
                          'call_not_started':True},ensure_ascii=False))
    finally:await d.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    asyncio.run(main(p.parse_args().base))
