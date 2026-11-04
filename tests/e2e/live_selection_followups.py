"""Регрессия реальных реплик о количестве и неподдержанных пожеланиях.

Настоящий Ultra, изолированный flexible_server --live-llm. Без звонков.
"""
import argparse
import asyncio
import json

from tests.e2e.live_dialogue_cases import Dialogue


async def main(base):
    d=Dialogue(base)
    try:
        await d.start('Москва, двухкомнатная до 20 млн рублей, вторичка, покупаю за свои средства.')
        assert (await d.round())['outcome']=='selection'
        reports=[]
        for text,limit in [
            ('выбери 3 самые выгодные',3),
            ('Эти мне не очень понравились. Давай 4 самые большие по убыванию площади',4),
            ('4 а не 3',4),
        ]:
            start=len(d.messages)
            await d.send(text)
            result=await d.round()
            assert result['outcome']=='selection',result
            assert len(d.cards)==limit,(text,len(d.cards))
            assert d.profile['rooms']==2 and d.profile['housing']=='secondary',d.profile
            assert d.profile['budget_max']==20_000_000
            assert any(f'Отобрал для вас {limit}' in message for message in d.messages[start:]),d.messages[start:]
            if limit==4:
                areas=[x['area'] for x in d.cards]
                assert areas==sorted(areas,reverse=True),areas
            reports.append({'input':text,'cards':limit,'rooms':d.profile['rooms'],'chat_ack':True})
            print(json.dumps(reports[-1],ensure_ascii=False),flush=True)
        start=len(d.messages)
        await d.send('Хочу квартиру где внутри бассейн')
        result=await d.round()
        assert result['outcome']=='clarify',result
        messages=' '.join(d.messages[start:]).casefold()
        assert 'не умею' in messages and 'бассейн' in messages and 'просторные' in messages,messages
        assert 'fitness' not in d.profile.get('poi',[]),d.profile
        await d.send('Тогда покажи 4 самые большие, остальные параметры сохрани.')
        result=await d.round()
        assert result['outcome']=='selection' and len(d.cards)==4,result
        assert d.profile['rooms']==2 and d.profile['budget_max']==20_000_000
        assert any('не проверено' in text.casefold() and 'бассейн' in text.casefold() for text in d.messages),d.messages[-4:]
        reports.append({'input':'unsupported pool -> accepted alternative','cards':4,'honest_limitation':True})
        print(json.dumps(reports[-1],ensure_ascii=False),flush=True)
        await d.send('Бассейн больше не нужен. Покажи 3 самых выгодных.')
        result=await d.round()
        assert result['outcome']=='selection' and len(d.cards)==3,result
        reports.append({'input':'remove unsupported condition','cards':3})
        start=len(d.messages)
        await d.send('Нужна собственная сауна внутри квартиры.')
        result=await d.round()
        assert result['outcome']=='clarify',result
        assert any('не умею' in message.casefold() and 'саун' in message.casefold() for message in d.messages[start:]),d.messages[start:]
        reports.append({'input':'different unsupported condition: sauna','honest_limitation':True})
        print(json.dumps({'passed':len(reports)},ensure_ascii=False),flush=True)
    finally:
        await d.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:8797')
    asyncio.run(main(parser.parse_args().base))
