"""Живой контроль FAQ, вопросительной корректировки и остановки без поиска/звонков."""
import argparse
import asyncio,json
from copy import deepcopy
from tests.e2e.live_dialogue_cases import Dialogue,_timeout

async def main(base):
 d=Dialogue(base)
 try:
  await d.start('Самара, двушка до 9 млн, наличными, только вторичка. Ремонт любой.')
  first=await d.round()
  assert first['outcome']=='selection', first
  before=deepcopy(d.profile)
  reply=await d.send('Что такое эскроу?')
  assert reply['handled_as']=='question', reply
  async with _timeout(45):
   while True:
    ev=await d.events.get()
    assert ev['type']!='resolved', 'Информационный вопрос закрыл карточку'
    assert not (ev['type']=='result' and ev.get('kind') in ('profile','search','selection')), 'Вопрос изменил подбор'
    if ev['type']=='message' and ev.get('role')=='agent':
     assert 'эскроу' in ev['text'].casefold(), ev['text']
     break
  reply=await d.send('Можно вместо Самары смотреть Казань? Бюджет и остальные условия оставь прежними.')
  assert reply['handled_as']=='answer', reply
  result=await d.round()
  assert result['outcome']=='selection',result
  assert d.profile['city']=='Казань'
  for key in ('rooms','budget_max','payment','housing'):assert d.profile[key]==before[key],key
  reply=await d.send('Пока больше ничего не ищи и никому не звони. Я подумаю.')
  assert reply['handled_as'] in ('answer','message'), reply
  async with _timeout(50):
   while True:
    ev=await d.events.get()
    assert not (ev['type']=='search_progress' and ev.get('on')), 'Поиск после остановки'
    assert ev['type']!='call_start', 'Звонок после остановки'
    assert not (ev['type']=='result' and ev.get('kind')=='search'), 'Поиск после остановки'
    if (ev['type']=='message' and ev.get('await_reply')) or (ev['type']=='confirm' and ev.get('kind')=='clarify'):break
  print(json.dumps({'ok':True,'session':d.sid,'faq_kept_selection':True,'question_shaped_refinement':'Казань','budget_kept':d.profile['budget_max'],'hold_no_search_or_calls':True},ensure_ascii=False))
 finally:await d.close()

if __name__=='__main__':
 parser=argparse.ArgumentParser()
 parser.add_argument('--base',required=True)
 asyncio.run(main(parser.parse_args().base))
