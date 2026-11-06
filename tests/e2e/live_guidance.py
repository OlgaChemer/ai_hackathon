"""Консультативный подбор с настоящей моделью; без разрешения звонков.

Стенд flexible_server --live-llm подменяет объявления, но не решения модели.
--case intake безопасно проверяет только вопросы на публичном сайте.
"""
import argparse
import asyncio
import json
from tests.e2e.live_dialogue_cases import Dialogue
from tests.e2e.live_budget_and_revisions import next_step


def question(e,subject):
    assert e.get('kind')=='clarify' and subject in e['text'].casefold(),e


async def intake(base):
    d=Dialogue(base)
    try:
        await d.start('Хочу квартиру в тюмени ближе к центру')
        e=await next_step(d);question(e,'сумм')
        assert not d.cards and d.profile['city']=='Тюмень',d.profile
        assert not any(x.get('kind')=='search' for x in d.timeline),d.timeline
        await d.send('На покупку есть 12 миллионов рублей.')
        e=await next_step(d);question(e,'комнат')
        assert not d.cards and d.profile['budget_max']==12_000_000,d.profile
        print(json.dumps({'case':'tyumen_intake','ok':True,'session':d.sid},ensure_ascii=False),flush=True)
    finally:await d.close()


async def help_unknown(base):
    d=Dialogue(base)
    try:
        await d.start('Хочу квартиру в Москве.')
        e=await next_step(d);question(e,'сумм')
        await d.send('Пока не знаю')
        e=await next_step(d);question(e,'бюджет')
        assert d.profile['budget_max'] is None and d.profile['market']['count']>=6,d.profile
        assert d.profile['market']['scope']['research']['group_by']=='rooms',d.profile
        assert any('медиана' in m for m in d.messages),d.messages
        await d.send('Пока не знаю')
        e=await next_step(d);question(e,'комнат')
        await d.send('Пока не знаю')
        e=await next_step(d);question(e,'комнат')
        assert d.profile['rooms'] is None and d.profile['budget_max'] is None,d.profile
        await d.send('Пока не знаю')
        e=await next_step(d)
        assert e.get('kind')=='selection' and d.cards,(e,d.messages[-3:])
        assert d.profile['rooms'] is None and d.profile['budget_max'] is None,d.profile
        print(json.dumps({'case':'unknown_parameters_help_to_examples','ok':True,'cards':len(d.cards),'session':d.sid},ensure_ascii=False),flush=True)
    finally:await d.close()


async def known_budget(base):
    d=Dialogue(base)
    try:
        await d.start('Ищу квартиру в Москве, на покупку есть 20 миллионов рублей.')
        e=await next_step(d);question(e,'комнат')
        await d.send('Пока не знаю')
        e=await next_step(d);question(e,'комнат')
        assert d.profile['budget_max']==20_000_000,d.profile
        assert d.profile['market']['scope']['budget_applied'],d.profile
        assert d.profile['market']['scope']['research']['group_by']=='rooms',d.profile
        await d.send('Я не знаю цены, помоги понять что могу купить.')
        e=await next_step(d)
        assert d.profile['budget_max']==20_000_000,(e,d.profile)
        print(json.dumps({'case':'known_budget_preserved_during_help','ok':True,'session':d.sid},ensure_ascii=False),flush=True)
    finally:await d.close()


async def main(base,case):
    await intake(base)
    if case=='all':
        await help_unknown(base)
        await known_budget(base)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8797')
    p.add_argument('--case',choices=['intake','all'],default='all');a=p.parse_args()
    asyncio.run(main(a.base,a.case))
