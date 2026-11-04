"""Сценарий скриншотов: цена метра -> две большие -> сравнение -> продолжение.

Может работать с настоящими источниками; телефоны не набирает. Тестовые
условия и подтверждения задаются явно, ошибочные ответы не переписываются.
"""
import argparse
import asyncio
import json
from copy import deepcopy

from tests.e2e.live_dialogue_cases import Dialogue


async def sequence(base):
    d=Dialogue(base)
    try:
        await d.start('Нижний Новгород, 1–2 комнаты до 6,7 млн рублей, только вторичка. Покупаю за свои средства.')
        outcome=await d.round()
        assert outcome['outcome']=='selection' and d.cards,outcome
        assert d.profile['rooms_options']==[1,2],d.profile
        for text,limit,field,reverse in [
            ('сделать 3 самые выгодные за квадратный метр',3,'price_per_m2',False),
            ('не, давай 2 самых больщих',2,'area',True),
        ]:
            await d.send(text);outcome=await d.round()
            assert outcome['outcome']=='selection' and len(d.cards)==limit,outcome
            vals=[x['price']/x['area'] if field=='price_per_m2' else x[field] for x in d.cards]
            assert vals==sorted(vals,reverse=reverse),vals
            assert d.profile['rooms_options']==[1,2]
            print(json.dumps({'input':text,'cards':len(d.cards),'ok':True},ensure_ascii=False),flush=True)
        saved=deepcopy(d.cards)
        start=len(d.timeline);start_messages=len(d.messages)
        await d.send('среди этих двух какая выгоднее за кв м');outcome=await d.round()
        assert outcome['outcome']=='selection',outcome
        assert not any(e['type']=='result' and e.get('kind')=='selection' for e in d.timeline[start:]),'comparison replaced cards'
        text=' '.join(d.messages[start_messages:])
        assert '₽/м²' in text and 'Карточки оставил' in text,text
        assert 'вариантов нет' not in text,text
        d.cards=saved  # клиент сохраняет видимый снимок при операции чтения
        await d.send('давай 2 самых больших квартиры');outcome=await d.round()
        assert outcome['outcome']=='selection' and len(d.cards)==2,outcome
        start=len(d.timeline)
        saved=deepcopy(d.cards)
        await d.send('Теперь нужна общая площадь не меньше 10000 квадратных метров.');outcome=await d.round()
        assert outcome['outcome']=='clarify',outcome
        assert not any(e['type']=='result' and e.get('kind')=='selection' for e in d.timeline[start:]),'empty query erased selection'
        assert 'площад' in outcome.get('text','').casefold(),outcome
        d.cards=saved
        await d.send('Отмени ограничение площади. Покажи две самые большие квартиры.');outcome=await d.round()
        assert outcome['outcome']=='selection' and len(d.cards)==2,outcome
        assert d.profile['rooms_options']==[1,2] and d.profile['budget_max']==6_700_000,d.profile
        print(json.dumps({'sequence':'comparison, retained snapshot, empty result, recovery','ok':True,'session':d.sid},ensure_ascii=False),flush=True)
    finally:await d.close()


async def clarification(base):
    d=Dialogue(base)
    try:
        await d.start('1-2 комнаты ближе к центральной улице нижнего')
        outcome=await d.round()
        assert outcome['outcome']=='clarify',outcome
        assert any(word in outcome['text'].casefold() for word in ('город','улиц','нижний')),outcome
        await d.send('Нижний Новгород. Можно ориентироваться на центр города. Нужны 1–2 комнаты до 6,7 млн, вторичка, за свои средства.')
        outcome=await d.round()
        assert outcome['outcome']=='selection' and d.cards,outcome
        assert d.profile['rooms_options']==[1,2] and d.profile['city']=='Нижний Новгород',d.profile
        print(json.dumps({'sequence':'specific clarification, no studio, continuation','ok':True,'session':d.sid},ensure_ascii=False),flush=True)
    finally:await d.close()


async def main(base):
    await sequence(base)
    await clarification(base)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    asyncio.run(main(p.parse_args().base))
