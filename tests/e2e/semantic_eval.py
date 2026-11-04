"""Корпус формулировок: реальная модель, синтетический контекст, без поиска/звонков.

Проверяет смысл и отсутствие лишних изменений отдельно от тестов исполнителя.
Любой не прошедший разбор считается провалом; дополнительные пользовательские
подсказки не добавляются. Используется серверный ключ, расходует квоту.
"""
import argparse
import asyncio
import json

from backend import db
from backend.llm import get_llm
from backend.agent import session,understanding as U,listing_query as Q
from backend.agent.profile import merge_profile
from tests.test_llm_and_agent import listing


CASES=[
 ('rank','сделать 3 самые выгодные за квадратный метр',3,'price_per_m2','asc','all'),
 ('rank','не, давай 2 самых больщих',2,'area','desc','all'),
 ('compare','среди этих двух какая выгоднее за кв м',None,'price_per_m2','asc','shown'),
 ('compare','Какая из этих квартир дешевле за квадратный метр?',None,'price_per_m2','asc','shown'),
 ('compare','Из показанных двух где метр стоит меньше?',None,'price_per_m2','asc','shown'),
 ('compare','А из них какая самая просторная?',None,'area','desc','shown'),
 ('compare','Сравни эти две по цене за метр',None,'price_per_m2','asc','shown'),
 ('compare','У какой из двух предложенных ниже стоимость метра?',None,'price_per_m2','asc','shown'),
 ('compare','Где из этих двух больше площадь?',None,'area','desc','shown'),
 ('rank','Покажи две самые большие квартиры',2,'area','desc','all'),
 ('rank','Оставь пять самых дешёвых',5,'price','asc','all'),
 ('rank','Выведи три наиболее выгодных',3,'value_score','desc','all'),
 ('rank','Понравившиеся отсортируй от большей площади к меньшей',None,'area','desc','liked'),
 ('rank','4 а не 3',4,'area','desc','all'),
 ('more','Хочу ещё варианты',None,None,None,'all'),
 ('more','Подбери новые предложения с учётом отметок',None,None,None,'all'),
 ('analyze','Посчитай среднюю цену за метр у этих двух',None,None,None,'shown'),
 ('question','В смысле? А что сломалось',None,None,None,None),
 ('question','А что надо то?',None,None,None,None),
 ('rooms','Ищу 1–2 комнаты в Нижнем Новгороде',None,None,None,None),
 ('rooms','Нижний Новгород, однушку или двушку. Студии не нужны.',None,None,None,None),
 ('studio','Студия в Нижнем Новгороде',None,None,None,None),
 ('mixed','Студия или однокомнатная в Нижнем Новгороде',None,None,None,None),
 ('clarify','1-2 комнаты ближе к центральной улице нижнего',None,None,None,None),
 ('clarify','Двушка в Новгороде',None,None,None,None),
 ('unsupported','Хочу квартиру где внутри бассейн',None,None,None,None),
 ('unsupported','Нужна собственная сауна внутри квартиры',None,None,None,None),
 ('full_profile','Ищу двухкомнатную квартиру в центре Москвы до 30 млн рублей, в новостройке. Покупаю за свои. Рядом нужен детский сад.',None,None,None,None),
 ('full_profile_nizhny','Нижний Новгород, 1–2 комнаты до 6,7 млн рублей, только вторичка. Покупаю за свои средства.',None,None,None,None),
]


async def run(cases):
    db.configure('sqlite://')
    llm=get_llm()
    assert llm.name=='gigachat'
    failures=[]
    for i,(kind,text,limit,field,direction,scope) in enumerate(cases):
        s=session.create_session('',llm)
        calls=[]
        class Trace:
            async def chat(self,*args,**kwargs):
                reply=await llm.chat(*args,**kwargs)
                calls.extend({'name':c.name,'arguments':c.arguments} for c in reply.tool_calls)
                if kwargs.get('tag')=='profile_extract':
                    try:calls.append({'tag':'profile_extract','frame':json.loads(reply.text)})
                    except ValueError:calls.append({'tag':'profile_extract','invalid_json':True})
                return reply
        s.llm=Trace()
        s.profile=merge_profile({'city':'Нижний Новгород','rooms_options':[1,2],'budget_max':6_700_000,'payment':'cash','housing':'secondary'})
        s.selection_query={**Q.default_query(),'limit':2,'sort':[{'field':'area','direction':'desc'}]}
        s.shown=[listing(id='cian-eval-1',city='Нижний Новгород',rooms=1,area=40),
                 listing(id='cian-eval-2',city='Нижний Новгород',rooms=2,area=80)]
        s.search={'pool':list(s.shown)}
        s.prefs.sync(s.profile)
        s.last_issue={'kind':'previous_error','message':'Предыдущее сравнение завершилось ошибкой системы. Последние две карточки сохранены.'}
        if kind in ('clarify','rooms','studio','mixed','full_profile','full_profile_nizhny'):s.profile=merge_profile({});s.search=None;s.shown=[]
        s.messages=[{'role':'user','content':'Покажи два самых больших варианта'}] if s.shown else []
        e=s.push('message',role='user',text=text);s.accept_request(text,e['seq'])
        try:
            r=await U.prepare(s)
            if kind in ('rank','compare','more','analyze'):
                assert r['decision']!='clarify',r
                assert r.get('selection_operation')==kind,r
                q=r['selection'];assert q and q['filters']==[],r
                assert not r['changes'],r
                if limit is not None:assert q['limit']==limit,r
                if field:assert q['sort'] and q['sort'][0]=={'field':field,'direction':direction},r
                assert q['scope']==scope,r
                if kind=='analyze':assert any(a['field']=='price_per_m2' and a['function']=='mean' for a in q['aggregates']),r
            elif kind=='question':assert r['intent']=='question' and not r['changes'],r
            elif kind=='clarify':assert r['decision']=='clarify' and r['question'].strip() and r['question']!='Уточните запрос',r
            elif kind=='unsupported':assert any(x['action']=='add' for x in r.get('unsupported') or []),r
            elif kind in ('full_profile','full_profile_nizhny'):
                p=merge_profile(r['changes'])
                assert p['payment']=='cash',r
                if kind=='full_profile':
                    assert p['rooms_options']==[2] and p['city']=='Москва' and p['housing']=='new' and p['budget_max']==30_000_000,r
                    assert p['center_required'] and 'kindergarten' in p['poi'],r
                    assert not any(f['field']=='delivery_year' for f in p['filters']),r
                else:assert p['rooms_options']==[1,2] and p['city']=='Нижний Новгород' and p['housing']=='secondary' and p['budget_max']==6_700_000,r
            else:
                p=merge_profile(r['changes'])
                expected={'rooms':[1,2],'studio':[0],'mixed':[0,1]}[kind]
                assert p['rooms_options']==expected,(p,r)
            print(json.dumps({'case':i+1,'input':text,'ok':True},ensure_ascii=False),flush=True)
        except Exception as exc:
            failures.append(i+1)
            print(json.dumps({'case':i+1,'input':text,'ok':False,'error':str(exc)[:1200],'calls':calls},ensure_ascii=False),flush=True)
        finally:session.SESSIONS.pop(s.id,None)
    print(json.dumps({'passed':len(cases)-len(failures),'total':len(cases),'failed_cases':failures}),flush=True)
    return bool(failures)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',type=int);args=p.parse_args()
    raise SystemExit(asyncio.run(run([CASES[args.case-1]] if args.case else CASES)))
