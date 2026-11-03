"""Настоящий Ultra разбирает примеры, прочитанные прямо из начального экрана.

Не создаёт диалогов в БД, не ищет объявления и не звонит. Расходует квоту модели.
Запуск: python -m tests.e2e.live_welcome_inputs [--repeat 3]
"""
import argparse
import asyncio
import json
import logging
from html.parser import HTMLParser
from pathlib import Path

from backend.agent.session import Session
from backend.agent import understanding
from backend.llm import build_llm


class Examples(HTMLParser):
    def __init__(self):
        super().__init__()
        self.queries=[]

    def handle_starttag(self, tag, attrs):
        query=dict(attrs).get('data-welcome-example')
        if query and query not in self.queries:self.queries.append(query)


async def main(repeat):
    logging.basicConfig(level=logging.INFO)
    page=Examples()
    page.feed((Path(__file__).resolve().parents[2]/'frontend/index.html').read_text())
    assert len(page.queries)==6, 'Изменились примеры на главной — обновите ожидания теста'
    expected=[('Москва',2,None),('Москва',2,None),('Москва',2,None),('Москва',2,None),('Казань',1,None),('Москва',2,15_000_000)]
    cases=[*zip(page.queries,expected),*[(page.queries[-1],expected[-1])]*(repeat-1)]
    llm=build_llm('gigachat')
    good=0
    for i,(text,(city,rooms,budget)) in enumerate(cases):
        s=Session(id=f'welcome-live-{i}',query='',llm=llm)
        s.request_inputs=[{'seq':1,'text':text,'question':'','options':[]}]
        try:
            review=await understanding.prepare(s)
            changes=review['changes']
            assert review['decision']=='apply',review['decision']
            assert changes.get('city')==city, 'Город'
            assert (changes.get('rooms_options') or [changes.get('rooms')])==[rooms], 'Комнаты'
            assert changes.get('budget_max')==budget, 'Бюджет'
            if i==2:assert {'park','school'}<=set(changes.get('poi',[])), 'Парк и школа'
            if i==3:assert changes.get('payment')=='mortgage_saving', 'Накопление взноса'
            good+=1
            print(json.dumps({'ok':True,'input':text,'changes':changes},ensure_ascii=False),flush=True)
        except Exception as exc:
            detail=str(exc) if isinstance(exc,AssertionError) or str(exc).startswith('GigaChat 400:') else type(exc).__name__
            print(json.dumps({'ok':False,'input':text,'error':detail},ensure_ascii=False),flush=True)
    print(f'ИТОГО: {good}/{len(cases)}',flush=True)
    return 0 if good==len(cases) else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repeat',type=int,default=3,choices=range(1,11))
    raise SystemExit(asyncio.run(main(parser.parse_args().repeat)))
