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
            patch.object(service.geo,'nearby',new=AsyncMock(return_value=False)),
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

    def test_operator_health_enables_phone_without_opening_public_access(self):
        from backend import phone
        with patch.object(phone,'settings',replace(phone.settings,phone_pin='',exolve_api_key='fixture')):
            self.assertTrue(self.client.get('/api/health').json()['phone']['ready'])
            response=self.client.post('/api/phone',json={'chat_id':'own','decision_id':'not-pending','destination':'79991234567'})
            self.assertNotEqual(response.status_code,403,response.text)
            self.client.post('/api/auth/logout')
            self.assertFalse(self.client.get('/api/health').json()['phone']['ready'])
            self.assertEqual(self.client.post('/api/phone',json={'chat_id':'own','decision_id':'not-pending','destination':'79991234567'}).status_code,403)

    def test_favorites_and_math_come_from_owned_chat_without_uploads(self):
        context=self.client.get('/api/workshop/context?chat_id=own').json()
        self.assertEqual([x['id'] for x in context['liked']],['a','b'])
        r=self.client.post('/api/workshop/jobs',json=self.payload());self.assertEqual(r.status_code,202,r.text)
        result=self.wait(r.json()['id']);self.assertEqual(result['status'],'completed',result)
        facts=result['result']['facts'];self.assertEqual(facts['version'],2)
        variants=facts['rows'][0]['scenarios']
        self.assertEqual([v['key'] for v in variants],['minimal','cosmetic','capital'])
        self.assertEqual((variants[0]['cost_min'],variants[0]['cost_max']),(287500,575000))
        self.assertNotIn('total',facts['rows'][0])
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

    def test_compare_has_real_characteristics_and_no_repair_costs(self):
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data']['shown'][0]['metro']={'name':'Примерная','minutes':7,'mode':'пешком'}
            saved['data']['shown'][0]['condition']='косметический'
            chat.state=saved
        job=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('compare')).json()['id'])
        self.assertEqual(job['status'],'completed',job)
        facts=job['result']['facts'];row=facts['rows'][0]
        self.assertEqual(row['metro']['minutes'],7)
        self.assertEqual(row['condition'],'косметический')
        self.assertEqual(row['price_per_m2'],200000)
        self.assertFalse(set(row)&{'repair','repair_with_reserve','total','scenarios'})
        self.assertEqual(facts['assumptions'],{})
        self.assertIn('criteria',row)
        self.assertEqual(facts['leaders']['price']['listing_ids'],['a'])
        self.assertEqual(facts['leaders']['price_per_m2']['listing_ids'],['b'])

    def test_new_market_uses_latest_accepted_conditions_and_strips_old_internal_query(self):
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            profile=merge_profile({'rooms_options':[3]},saved['data']['profile'])
            profile['summary']='Ищу 2-комнатную квартиру'
            profile['_initial_request']='Ищу двушку'
            saved['data'].update(profile=profile,profile_revision=8)
            chat.state=saved
        ctx=self.client.get('/api/workshop/context?chat_id=own').json()
        self.assertEqual(ctx['profile']['rooms'],3)
        self.assertIn('3-комнат',ctx['profile']['summary'])
        self.assertNotIn('_initial_request',ctx['profile'])
        rows=[listing(id=str(i),city='Москва',rooms=3) for i in range(8)]
        with patch.object(service.sources,'collect',AsyncMock(return_value=(rows,[]))) as collect:
            job=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market')).json()['id'])
            self.assertEqual(job['status'],'completed',job)
            self.assertEqual(collect.call_args.args[0]['rooms_options'],[3])
        self.assertEqual(self.calls[-1]['revision'],8)

    def test_market_waits_for_pending_profile_change_instead_of_using_previous_rooms(self):
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data'].update(request_inputs=[{'seq':9,'text':'Теперь трёшку'}],request_review_seq=8)
            chat.state=saved
        self.assertTrue(self.client.get('/api/workshop/context?chat_id=own').json()['updating'])
        r=self.client.post('/api/workshop/jobs',json=self.payload('market'))
        self.assertEqual(r.status_code,409,r.text)
        self.assertEqual(self.calls,[])

    def test_context_exposes_finance_and_latest_filters_before_market_research(self):
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data']['profile']=merge_profile({'rooms_options':[3],'budget_max':42_000_000,
                'payment':'mortgage_saving','apartments':'hide','filters':[
                    {'field':'delivery_year','op':'eq','values':['2027'],'priority':'required'},
                    {'field':'floor','op':'gt','values':['10'],'priority':'required'}]},saved['data']['profile'])
            saved['data']['profile_revision']=9
            chat.state=saved
        ctx=self.client.get('/api/workshop/context?chat_id=own').json()
        self.assertEqual(ctx['revision'],9)
        self.assertEqual(ctx['profile']['rooms_options'],[3])
        self.assertEqual(ctx['profile']['budget_max'],42_000_000)
        details=' '.join(ctx['conditions'])
        for text in ('взнос ещё копим','Маткапитал: ещё не уточнён','2027','10','Апартаменты исключены'):
            self.assertIn(text,details)

    def test_market_keeps_both_markets_and_apartments_when_client_has_no_preference(self):
        profile=merge_profile({'city':'Москва','rooms':3,'budget_min':29_400_000,'budget_max':42_000_000,
            'housing':'both','apartments':'any','payment':'mortgage_saving'})
        rows=[listing(id='mixed-'+str(i),city='Москва',rooms=3,price=35_000_000,
            is_new=bool(i%2),is_apartments=bool((i//2)%2)) for i in range(8)]
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data'].update(profile=profile,profile_revision=10,search={'listings':rows},shown=rows,prefs={'liked':[]})
            chat.state=saved
        ctx=self.client.get('/api/workshop/context?chat_id=own').json()
        details=' '.join(ctx['conditions'])
        self.assertIn('Квартиры и апартаменты',details)
        self.assertIn('новостройки и вторичка — без предпочтения',details)
        with patch.object(service.sources,'collect',new=AsyncMock()) as collect:
            job=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market',liked=[])).json()['id'])
            self.assertEqual(job['status'],'completed',job)
            collect.assert_not_awaited()
        snapshot=self.calls[-1]
        self.assertEqual(snapshot['profile']['housing'],'both')
        self.assertEqual(snapshot['profile']['apartments'],'any')
        self.assertEqual({(x['is_new'],x['is_apartments']) for x in snapshot['listings']},
            {(False,False),(False,True),(True,False),(True,True)})

    def test_market_reuses_all_search_results_and_keeps_unknowns_out_of_statistics(self):
        profile=merge_profile({'city':'Москва','rooms':2,'poi':['park']})
        rows=[listing(id=str(i),city='Москва',nearby={'park':{'name':'Парк','dist':400}} if i<4 else {},
                      price=8_000_000+i*100000) for i in range(6)]
        # Релевантные квартиры за пределами старого лимита первых 150 строк.
        irrelevant=[listing(id='other-'+str(i),city='Москва',rooms=1) for i in range(160)]
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data'].update(profile=profile,shown=rows,search={'listings':irrelevant+rows},prefs={'liked':['0']})
            chat.state=saved
        with patch.object(service.sources,'collect',new=AsyncMock()) as collect:
            result=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market',liked=[])).json()['id'])
            collect.assert_not_awaited()
        self.assertEqual(result['status'],'completed',result)
        sample=self.calls[-1];facts=result['result']['facts'];coverage=facts['coverage']
        self.assertEqual((coverage['received'],coverage['confirmed'],coverage['unverified']),(166,4,2))
        self.assertEqual(facts['count'],4)
        self.assertEqual(facts['median_price'],8_150_000)
        self.assertEqual(facts['rows'][0]['price_per_m2'],round(rows[0]['price']/rows[0]['area']))
        self.assertEqual(len(facts['rows']),4)
        self.assertEqual({x['id'] for x in sample['listings']},{'0','1','2','3'})
        self.assertEqual({x['id'] for x in sample['unverified_listings']},{'4','5'})
        self.assertEqual(coverage['origin'],'current_search')
        self.assertTrue(coverage['small_sample'])
        service.geo.nearby.assert_not_awaited()

    def test_market_current_conditions_and_snapshot_survive_later_profile_changes(self):
        with patch.object(service,'launch'):
            root=self.client.post('/api/workshop/jobs',json=self.payload('market')).json()
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state)
            saved['data']['profile']=merge_profile({'rooms_options':[3]},saved['data']['profile'])
            saved['data']['search']={'listings':[]};saved['data']['shown']=[];chat.state=saved
        with patch.object(service.sources,'collect',new=AsyncMock()) as collect:
            self.client.portal.call(service.execute,root['id'])
            collect.assert_not_awaited()
        result=self.wait(root['id']);self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['result']['facts']['count'],2)
        self.assertEqual(self.calls[-1]['profile']['rooms'],2)

    def test_market_fresh_search_filters_before_map_and_sample_limit(self):
        profile=merge_profile({'city':'Москва','rooms_options':[3],'poi':['park']})
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state);saved['data']['profile']=profile;chat.state=saved
        irrelevant=[listing(id='other-'+str(i),city='Москва',rooms=1) for i in range(160)]
        candidates=[listing(id='new-'+str(i),city='Москва',rooms=3,nearby={}) for i in range(6)]
        async def nearby(rows,kinds,**kw):
            self.assertEqual({x['id'] for x in rows},{x['id'] for x in candidates})
            for x in rows:x['nearby']={'park':{'name':'Парк','dist':400}}
            return True
        with patch.object(service.sources,'collect',new=AsyncMock(return_value=(irrelevant+candidates,[]))),\
             patch.object(service.geo,'nearby',new=AsyncMock(side_effect=nearby)):
            result=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market')).json()['id'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['result']['facts']['count'],6)
        self.assertEqual(result['result']['facts']['coverage']['origin'],'fresh_search')

    def test_market_one_confirmation_is_small_sample_not_failure(self):
        rows=[listing(id='one',city='Москва')]
        with patch.object(service.sources,'collect',new=AsyncMock(return_value=(rows,[]))):
            result=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market',whole_city=True)).json()['id'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['result']['facts']['count'],1)
        self.assertTrue(result['result']['facts']['coverage']['small_sample'])

    def test_market_empty_confirmation_preserves_coverage_and_names_unknowns(self):
        profile=merge_profile({'city':'Москва','rooms':2,'poi':['park']})
        with db.session() as s:
            chat=s.get(Chat,'own');saved=deepcopy(chat.state);saved['data']['profile']=profile;chat.state=saved
        rows=[listing(id='unknown',city='Москва',nearby={})]
        with patch.object(service.sources,'collect',new=AsyncMock(return_value=(rows,[]))):
            result=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market')).json()['id'])
        self.assertEqual(result['status'],'failed',result)
        self.assertIn('требуют проверки: 1',result['error'])
        self.assertNotIn('хотя бы 6',result['error'])
        with db.session() as s:
            sample=s.get(WorkshopJob,result['id']).snapshot
            self.assertEqual(sample['coverage']['unverified'],1)
            self.assertEqual(sample['listings'],[])
        self.assertEqual(self.calls,[])

    def test_duplicate_id_returns_same_job_and_changed_payload_conflicts(self):
        body=self.payload();first=self.client.post('/api/workshop/jobs',json=body).json()
        self.wait(first['id'])
        second=self.client.post('/api/workshop/jobs',json=body)
        self.assertEqual(second.json()['id'],first['id']);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.client.post('/api/workshop/jobs',json={**body,'rate':50000}).status_code,409)

    def test_followup_uses_saved_data_and_restores_conversation_without_research(self):
        root=self.wait(self.client.post('/api/workshop/jobs',json=self.payload()).json()['id'])
        with db.session() as s:
            chat=s.get(Chat,'own');chat.state={};chat.profile=merge_profile({'city':'Казань'})
        question={'command_id':'followup-one','question':'Почему первая квартира выгоднее?'}
        response=self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json=question)
        self.assertEqual(response.status_code,202,response.text)
        child=self.wait(response.json()['id']);self.assertEqual(child['status'],'completed',child)
        snapshot=self.calls[-1];self.assertEqual(snapshot['listings'],self.calls[0]['listings'])
        self.assertEqual(snapshot['source_time'],self.calls[0]['source_time'])
        self.assertEqual(snapshot['conversation'][-1]['content'],root['result']['text'])
        self.assertEqual(snapshot['prompt'],question['question'])
        same=self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json=question)
        self.assertEqual(same.json()['id'],child['id']);self.assertEqual(len(self.calls),2)
        second=self.client.post('/api/workshop/jobs/'+child['id']+'/followups',json={'command_id':'followup-two','question':'А если учитывать площадь?'})
        self.wait(second.json()['id'])
        thread=self.client.get('/api/workshop/jobs/'+root['id']+'/thread').json()
        self.assertEqual(len(thread['jobs']),3)
        self.assertEqual(thread['jobs'][1]['prompt'],question['question'])
        self.assertEqual(len(self.client.get('/api/workshop/jobs').json()['jobs']),1)
        self.assertEqual(self.calls[-1]['conversation'][-2]['content'],question['question'])

    def test_market_followup_does_not_fetch_new_listings(self):
        rows=[listing(id=str(i),city='Москва',rooms=2,price=8_000_000+i*100000) for i in range(8)]
        with patch.object(service.sources,'collect',new=AsyncMock(return_value=(rows,[]))) as collect:
            root=self.wait(self.client.post('/api/workshop/jobs',json=self.payload('market',whole_city=True)).json()['id'])
            self.assertEqual(root['status'],'completed',root)
            child=self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json={'command_id':'market-question','question':'Какая медианная цена?'})
            result=self.wait(child.json()['id']);self.assertEqual(result['status'],'completed',result)
            self.assertEqual(collect.await_count,1)
            self.assertEqual(result['result']['facts'],root['result']['facts'])

    def test_followup_rejects_foreign_pending_empty_and_offtopic_requests(self):
        root=self.wait(self.client.post('/api/workshop/jobs',json=self.payload()).json()['id'])
        with db.session() as s:
            u=User(email='foreign@example.ru',password_hash='unused');s.add(u);s.flush()
            s.add(Chat(id='foreign',user_id=u.id,title='Закрытый'));s.flush()
            s.add(WorkshopJob(id='foreign-job',user_id=u.id,chat_id='foreign',command_id='old',kind='repair',prompt='',provider='deepseek',status='completed',snapshot={},events=[],result={'text':'private'}))
        body={'command_id':'question','question':'Почему?'}
        self.assertEqual(self.client.get('/api/workshop/jobs/foreign-job/thread').status_code,404)
        self.assertEqual(self.client.post('/api/workshop/jobs/foreign-job/followups',json=body).status_code,404)
        self.assertEqual(self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json={**body,'question':'   '}).status_code,422)
        with db.session() as s:s.get(WorkshopJob,root['id']).status='running'
        self.assertEqual(self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json=body).status_code,409)
        with db.session() as s:s.get(WorkshopJob,root['id']).status='completed'
        child=self.client.post('/api/workshop/jobs/'+root['id']+'/followups',json={**body,'question':'Напиши шахматную игру'})
        self.assertEqual(self.wait(child.json()['id'])['status'],'refused')
        self.assertEqual(len(self.calls),1)

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
    def test_compaction_keeps_photo_evidence_after_later_python_runs(self):
        from workshop_runtime.history import summary
        snapshot={'prompt':'Сравни состояние','kind':'compare','facts':{'count':3}}
        photo={'listing_id':'flat','status':'analyzed','visible_state':'cosmetic','photos':[{'url':'https://example.invalid'}],
               'observations':['На фото потёртый пол'],'limitations':['Скрытая проводка не проверена']}
        history=[{'role':'tool','content':'Поздний расчёт '+str(i)+'x'*6000} for i in range(5)]
        result=json.loads(summary(snapshot,history,4,[photo]))
        self.assertEqual(result['visual_analysis'][0]['observations'],['На фото потёртый пол'])
        self.assertEqual(result['visual_analysis'][0]['photos_seen'],1)
        self.assertEqual(result['facts'],snapshot['facts'])
        large={**photo,'observations':['x'*5000]*20,'limitations':['y'*5000]*20}
        self.assertLess(len(summary(snapshot,history,4,[large]*20)),12000)
    def test_three_repair_ranges_preserve_unknown_area_and_exclude_purchase_budget(self):
        snapshot={'kind':'repair','analysis_version':2,'assumptions':data.analysis_assumptions('repair'),
                  'profile':{'budget_max':1},'listings':[listing(id='known',area=50),listing(id='unknown',area=None)]}
        facts=data.facts(snapshot)
        self.assertEqual(len(facts['rows'][0]['scenarios']),3)
        self.assertTrue(all(v['cost_min']>1 for v in facts['rows'][0]['scenarios']))
        self.assertTrue(all(v['cost_min'] is None and v['cost_max'] is None for v in facts['rows'][1]['scenarios']))
        self.assertNotIn('total',facts['rows'][0])

    def test_retry_replaces_readonly_snapshot_without_corrupting_previous_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            target=sandbox.save_input(root,{'prompt':'Первый запрос'})
            with target.open() as previous:
                self.assertEqual(target.stat().st_mode & 0o777,0o444)
                sandbox.save_input(root,{'prompt':'Повтор задания'})
                self.assertEqual(json.loads(previous.read()),{'prompt':'Первый запрос'})
            self.assertEqual(json.loads(target.read_text()),{'prompt':'Повтор задания'})
            with self.assertRaises(TypeError):sandbox.save_input(root,{'invalid':object()})
            self.assertEqual(json.loads(target.read_text()),{'prompt':'Повтор задания'})
            self.assertEqual(list(root.glob('.input-*')),[])
            self.assertEqual(target.stat().st_mode & 0o777,0o444)

    def test_long_research_history_is_bounded_without_losing_source_data(self):
        from workshop_runtime.history import summary,context_messages
        history=[]
        for i in range(4):history.extend([{'role':'user','content':f'Уточнение {i}: '+ 'вопрос '*130},
                                          {'role':'assistant','content':'Длинный отчёт '+ 'подробности '*500+f' Итог {i}'}])
        original=deepcopy(history)
        data={'prompt':'Теперь оцени фотографии','kind':'repair','facts':{'count':20,'total':19000000},'conversation':history}
        prepared=context_messages(data)
        self.assertLessEqual(sum(len(m['content']) for m in prepared),8000)
        self.assertLessEqual(len(json.dumps(prepared,ensure_ascii=False,separators=(',',':')).encode()),4200)
        self.assertTrue(any('Уточнение 3' in m['content'] for m in prepared if m['role']=='user'))
        self.assertEqual(json.loads(summary(data,[],3))['facts'],data['facts'])
        self.assertEqual(history,original)
        self.assertLess(len(summary(data,[],3)),10000)

    def test_large_market_context_is_bounded_in_utf8_bytes_and_keeps_all_source_rows(self):
        from workshop_runtime.history import summary,initial_task,facts_context
        rows=[{'id':f'квартира-{i}','address':'Длинная русская улица, '+str(i),
               'price':18_700_000+i,'area':52,'price_per_m2':359615} for i in range(150)]
        facts={'kind':'market','count':150,'median_price':18_700_075,'rows':rows,
            'groups':[{'rooms':3,'count':150,'median_price':18_700_075}]}
        snapshot={'prompt':'Исследуй рынок','kind':'market','facts':facts,
            'conversation':[{'role':'assistant','content':'Русский отчёт '*5000}]*20}
        original=deepcopy(snapshot)
        overview=facts_context(facts)
        self.assertEqual(overview['count'],150)
        self.assertEqual(overview['median_price'],18_700_075)
        self.assertEqual(overview['rows_count'],150)
        self.assertNotIn('rows',overview)
        self.assertLess(len(initial_task(snapshot).encode()),6500)
        compact=summary(snapshot,[{'role':'tool','content':'Большой вывод '*5000}]*20,3)
        self.assertLessEqual(len(compact.encode()),6500)
        self.assertEqual(json.loads(compact)['facts']['rows_count'],150)
        self.assertEqual(snapshot,original)

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
