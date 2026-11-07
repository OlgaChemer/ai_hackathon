"""Премиум-мастерская: права, исходные данные, выполнение, идемпотентность и модельный контракт."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select
from backend import auth,db,main,ratelimit
from backend.db.models import User,Chat,WorkshopJob
from backend.agent.profile import merge_profile
from backend.llm import LLMResponse,ToolCall
from backend.workshop import api,data,service,store,sandbox
from tests.test_llm_and_agent import listing


class ScopeLLM:
    name='fake';model='scope'
    async def chat(self,messages,**kw):
        off='шахмат' in messages[-1]['content'].casefold()
        return LLMResponse('',[ToolCall('task_scope',{'decision':'refuse' if off else 'allow',
            'message':'Я помогаю с недвижимостью. Могу сравнить квартиры или расходы на ремонт.'})])


class WorkshopAPI(unittest.TestCase):
    def setUp(self):
        db.configure('sqlite://');ratelimit.reset();auth._fails.clear()
        self.temp=tempfile.TemporaryDirectory();self.calls=[]
        self.settings=replace(api.settings,workshop_root=self.temp.name)
        self.patches=[patch.object(main.app.router,'on_startup',[]),patch.object(api,'settings',self.settings),
            patch.object(service,'get_provider',return_value=ScopeLLM()),patch.object(service.sandbox,'run',self.run_sandbox)]
        for p in self.patches:p.start()
        self.client=TestClient(main.app,base_url='https://testserver');self.client.__enter__()
        r=self.client.post('/api/auth/register',json={'email':'darkyhat@mail.ru','password':'workshop-test-123'})
        self.uid=r.json()['user']['id']
        with db.session() as s:s.get(User,self.uid).premium=True
        self.rows=[listing(id='a',city='Москва',area=50,price=10_000_000),listing(id='b',city='Москва',area=70,price=12_000_000)]
        with db.session() as s:
            s.add(Chat(id='own',user_id=self.uid,title='Москва',profile=merge_profile({'city':'Москва','rooms':2}),
                state={'format':1,'data':{'profile':merge_profile({'city':'Москва','rooms':2}),
                    'prefs':{'liked':['a','b']},'shown':self.rows,'search':{'listings':self.rows},'profile_revision':7}}))

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    async def run_sandbox(self,job_id,snapshot,llm,progress):
        self.calls.append(deepcopy(snapshot));progress('Выполняю проверенный расчёт.')
        root=Path(self.temp.name)/job_id/'artifacts';root.mkdir(parents=True)
        (root/'analysis.csv').write_text('Квартира,Стоимость\na,11725000')
        (root/'analysis.xlsx').write_bytes(b'fixture')
        return {'text':'Сравнение квартир и сценарных расходов на ремонт. Известная площадь учтена.',
            'facts':snapshot['facts'],'artifacts':['analysis.csv','analysis.xlsx'],'source_time':snapshot['source_time'],
            'code_runs':1,'data_revision':snapshot['revision']}

    def payload(self,kind='repair',**kw):return {'command_id':'test_'+kind,'kind':kind,'chat_id':'own',**kw}

    def wait(self,job_id):
        for _ in range(150):
            r=self.client.get('/api/workshop/jobs/'+job_id);self.assertEqual(r.status_code,200,r.text)
            value=r.json()
            if value['status'] not in ('queued','running'):return value
            time.sleep(.01)
        self.fail('Задание не завершилось')

    def test_only_granted_account_gets_premium_and_anonymous_has_no_access(self):
        self.assertTrue(self.client.get('/api/me').json()['user']['premium'])
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.get('/api/workshop/context').status_code,401)
        r=self.client.post('/api/auth/register',json={'email':'other@example.ru','password':'workshop-test-123'})
        other=r.json()['user']['id']
        with db.session() as s:s.get(User,other).premium=True
        self.assertFalse(self.client.get('/api/me').json()['user']['premium'])
        self.assertEqual(self.client.get('/api/workshop/context').status_code,403)
        self.assertEqual(self.client.post('/api/workshop/jobs',json=self.payload()).status_code,403)

    def test_favorites_and_math_come_from_owned_chat_without_uploads(self):
        context=self.client.get('/api/workshop/context?chat_id=own').json()
        self.assertEqual([x['id'] for x in context['liked']],['a','b'])
        r=self.client.post('/api/workshop/jobs',json=self.payload());self.assertEqual(r.status_code,202,r.text)
        result=self.wait(r.json()['id']);self.assertEqual(result['status'],'completed',result)
        facts=result['result']['facts'];self.assertEqual(facts['rows'][0]['repair_with_reserve'],1_725_000)
        self.assertEqual(facts['rows'][0]['total'],11_725_000)
        self.assertEqual(self.calls[0]['revision'],7)
        self.assertNotIn('provider',result)
        file=self.client.get('/api/workshop/jobs/'+result['id']+'/files/analysis.csv')
        self.assertEqual(file.status_code,200);self.assertIn('attachment',file.headers['content-disposition'])

    def test_visible_unsent_likes_can_select_only_objects_from_that_chat(self):
        r=self.client.post('/api/workshop/jobs',json=self.payload(liked=['b']))
        result=self.wait(r.json()['id']);self.assertEqual(result['result']['facts']['count'],1)
        self.assertEqual(self.calls[0]['listings'][0]['id'],'b')
        r=self.client.post('/api/workshop/jobs',json=self.payload(command_id='forged',liked=['not-owned']))
        self.assertEqual(r.status_code,422,r.text)

    def test_duplicate_id_returns_same_job_and_changed_payload_conflicts(self):
        body=self.payload();first=self.client.post('/api/workshop/jobs',json=body).json()
        self.wait(first['id'])
        second=self.client.post('/api/workshop/jobs',json=body)
        self.assertEqual(second.json()['id'],first['id']);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.client.post('/api/workshop/jobs',json={**body,'rate':50000}).status_code,409)

    def test_foreign_chat_job_and_download_are_not_accessible(self):
        with db.session() as s:
            u=User(email='foreign@example.ru',password_hash='unused');s.add(u);s.flush()
            s.add(Chat(id='foreign',user_id=u.id,title='Закрытый'));s.flush()
            s.add(WorkshopJob(id='foreign-job',user_id=u.id,chat_id='foreign',command_id='old',kind='repair',prompt='',provider='deepseek',status='completed',snapshot={},events=[],result={'artifacts':['analysis.csv']}))
        self.assertEqual(self.client.get('/api/workshop/context?chat_id=foreign').status_code,404)
        self.assertEqual(self.client.post('/api/workshop/jobs',json=self.payload(chat_id='foreign')).status_code,404)
        self.assertEqual(self.client.get('/api/workshop/jobs/foreign-job').status_code,404)
        self.assertEqual(self.client.get('/api/workshop/jobs/foreign-job/files/analysis.csv').status_code,404)

    def test_offtopic_never_launches_container(self):
        r=self.client.post('/api/workshop/jobs',json=self.payload(prompt='Для квартиры напиши шахматную игру'))
        result=self.wait(r.json()['id']);self.assertEqual(result['status'],'refused',result)
        self.assertEqual(self.calls,[])

    def test_market_has_real_source_coverage_and_does_not_change_profile(self):
        rows=[listing(id=str(i),city='Москва',rooms=1+i%2,price=8_000_000+i*100000) for i in range(10)]
        async def collect(p,on_batch=None):
            self.assertIsNone(p['rooms']);on_batch(rows)
            return rows,[{'source':'fixture','status':'live','count':10}]
        with patch.object(service.sources,'collect',side_effect=collect):
            r=self.client.post('/api/workshop/jobs',json=self.payload('market',whole_city=True))
            result=self.wait(r.json()['id'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['result']['facts']['count'],10)
        self.assertFalse(result['result']['facts']['whole_market'])
        self.assertEqual(len(result['result']['facts']['groups']),2)
        with db.session() as s:self.assertEqual(s.get(Chat,'own').profile['rooms'],2)

    def test_no_likes_does_not_pick_objects_without_user(self):
        r=self.client.post('/api/workshop/jobs',json=self.payload(liked=[]))
        self.assertEqual(r.status_code,422,r.text);self.assertEqual(self.calls,[])

    def test_provider_cannot_be_selected_from_browser_and_entitlement_can_be_revoked(self):
        self.assertEqual(self.client.post('/api/workshop/jobs',json=self.payload(provider='gigachat')).status_code,422)
        r=self.client.post('/api/workshop/jobs',json=self.payload());job=self.wait(r.json()['id'])
        with db.session() as s:s.get(User,self.uid).premium=False
        self.assertEqual(self.client.get('/api/workshop/jobs/'+job['id']).status_code,403)
        self.assertEqual(self.client.get('/api/workshop/jobs/'+job['id']+'/files/analysis.csv').status_code,403)


class WorkshopContracts(unittest.IsolatedAsyncioTestCase):
    def test_compaction_keeps_exact_task_and_facts_but_bounds_observations(self):
        from workshop_runtime.history import summary
        data={'prompt':'Сравни только эти квартиры, резерв 17%, не меняй бюджет',
              'kind':'compare','facts':{'rows':[{'id':'one','total':19000000},{'id':'two','total':None}]}}
        history=[{'role':'tool','content':str(i)+'x'*10000} for i in range(30)]
        compact=json.loads(summary(data,history,3))
        self.assertEqual(compact['task'],data['prompt'])
        self.assertEqual(compact['facts'],data['facts'])
        self.assertEqual(len(compact['last_observations']),3)
        self.assertTrue(all(len(x)<=1500 for x in compact['last_observations']))
        self.assertEqual(compact['successful_python_runs'],3)

    def test_csv_cells_cannot_turn_into_formulas(self):
        from backend.workshop.artifacts import prepare
        csv=prepare('analysis.csv',b'name,value\n=WEBSERVICE("https://example.org"),-15\n')
        self.assertIn("'=WEBSERVICE",csv.decode('utf-8-sig'))
        self.assertIn(',-15',csv.decode('utf-8-sig'))

    async def test_bridge_preserves_gigachat_function_state_and_limits_tools(self):
        class Model:
            def __init__(self):self.messages=[]
            async def chat(self,messages,**kwargs):
                self.messages.append(messages)
                return LLMResponse('Готово',[ToolCall('run_python',{'code':'print(1)'},'f1')],functions_state_id='state-1')
        model=Model();bridge=sandbox.Bridge(model,'Сравнить квартиры',lambda text:None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            r=await client.post('/chat',json={'messages':[{'role':'system','content':'Инструкции nanobot'},
                {'role':'user','content':'расчёт'}]})
            self.assertEqual(r.status_code,200)
            self.assertEqual(sum(m['role']=='system' for m in model.messages[0]),1)
            self.assertTrue(model.messages[0][0]['content'].startswith(sandbox.DOMAIN))
            self.assertIn('Инструкции nanobot',model.messages[0][0]['content'])
            await client.post('/chat',json={'messages':[{'role':'assistant','content':'','tool_calls':[{'id':'f1','function':{'name':'run_python','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'f1','content':'1'}]})
            self.assertEqual(model.messages[-1][1]['functions_state_id'],'state-1')
            await client.post('/chat',json={'messages':[{'role':'tool','tool_call_id':'f1','content':'Код возврата: 0'}]})
            self.assertEqual(json.loads(model.messages[-1][1]['content']),{'output':'Код возврата: 0'})
            self.assertEqual((await client.post('/chat',json={'messages':[{'role':'user','content':'test'}],
                'tools':[{'function':{'name':'call_seller','parameters':{}}}]})).status_code,403)
            bridge.calls=12
            self.assertEqual((await client.post('/chat',json={'messages':[{'role':'user','content':'test'}]})).status_code,429)

    def test_unknown_area_is_not_zero_and_recovery_does_not_repeat_work(self):
        snapshot={'kind':'repair','assumptions':data.assumptions(30000,15),'listings':[listing(id='x',area=None)]}
        facts=data.facts(snapshot);self.assertIsNone(facts['rows'][0]['repair_with_reserve']);self.assertIsNone(facts['rows'][0]['total'])
        db.configure('sqlite://')
        with db.session() as s:
            u=User(email='a@example.ru',password_hash='x');s.add(u);s.flush()
            s.add(Chat(id='x',user_id=u.id,title='x'));s.flush()
            s.add(WorkshopJob(id='x',user_id=u.id,chat_id='x',command_id='x',kind='repair',prompt='',provider='gigachat',status='running',events=[],snapshot={}))
        db.required(store.recover)
        with db.session() as s:self.assertEqual(s.get(WorkshopJob,'x').status,'interrupted')
