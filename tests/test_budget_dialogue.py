"""Бюджет неизвестен: вопрос должен закончиться данными, а не обещанием ответа."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
import json
import unittest
from unittest.mock import AsyncMock,patch

from backend import db,sources
from backend.agent import actions,loop,session,understanding,qa
from backend.agent.actions.request import apply_request
from backend.agent.profile import merge_profile
from backend.llm import LLMResponse,ToolCall
from backend.sources import market_estimate as market
from tests.fakes import ScriptedLLM,call,say
from tests.test_llm_and_agent import listing


class BudgetModel(ScriptedLLM):
    async def chat(self,messages,**kw):
        if kw.get('tag')=='profile_extract':
            return LLMResponse(json.dumps({'fields':[],'criteria':[],'states':[],'filters':[],
                'places':[],'unsupported':[],'selection':[],'requested_card':[]}))
        if kw.get('tag')=='request_review':
            payload=json.loads(next(m['content'] for m in messages if m['role']=='user'))['data']
            self.reviews.append(payload)
            return LLMResponse('',[ToolCall('request_information',{'action':'market_prices','update_conditions':False,
                'research':{'respect_budget':False,'group_by':None,'aggregates':[]}})])
        return await super().chat(messages,**kw)


class BudgetDialogue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self,model=None):
        s=session.create_session('',model or BudgetModel())
        s.profile=merge_profile({'city':'Москва','rooms':3,'housing':'both','payment':'cash','center_required':True,'prefer_center':True})
        s.prefs.sync(s.profile);s.remember()
        return s

    def prices(self,s):
        return {'profile_key':market.key(s.profile),'generated_at':datetime.now(timezone.utc).isoformat(),
                'p25':30_000_000,'median':40_000_000,'mean':41_000_000,'p75':50_000_000,'min':25_000_000,
                'ppm2':450_000,'count':24,'scope':market.scope(s.profile),'markets':[{'new':False,'count':12},{'new':True,'count':12}],
                'reports':[],'reasons':[]}

    async def wait(self,predicate,task):
        async def poll():
            while not predicate():
                if task.done():task.result();self.fail('Агент завершился до результата')
                await asyncio.sleep(.005)
        await asyncio.wait_for(poll(),3)

    async def test_unknown_budget_interrupts_wait_and_runs_existing_tool_once(self):
        model=BudgetModel(call('ask_client',question='Какой бюджет?'),say('Хотите ещё раз показать ориентиры?'),say('Какой лимит выберете?'))
        s=self.agent(model);original=deepcopy(s.profile)
        with patch.object(sources,'market',AsyncMock(return_value=self.prices(s))) as fetch:
            s.task=asyncio.create_task(loop.run_session(s))
            try:
                await self.wait(lambda:'clarify' in s.pending,s.task)
                first_id=s.pending_ids['clarify']
                self.assertEqual(await s.receive_text('Не знаю, какой средний?',command_id='average'),'answer')
                await self.wait(lambda:'clarify' in s.pending and s.pending_ids['clarify']!=first_id,s.task)
                self.assertEqual(fetch.await_count,1)
                self.assertEqual(s.profile,original)
                self.assertFalse(s.red_lines_ok)
                self.assertIn('С учётом этих ориентиров',s.pending_decisions['clarify']['text'])
                self.assertTrue(any(e['type']=='resolved' and e.get('decision_id')==first_id for e in s.history))
                self.assertTrue(any('40 000 000' in e.get('text','') for e in s.history))
                self.assertFalse(any(e.get('text')=='Хотите ещё раз показать ориентиры?' for e in s.history))
                self.assertIsNone(s.profile['budget_max'])
                self.assertEqual(len([o for o in s.operations.values() if o['name']=='market_prices']),1)
            finally:
                s.task.cancel();await asyncio.gather(s.task,return_exceptions=True)

    async def test_short_agreement_after_restart_uses_saved_qa_proposal(self):
        s=self.agent();s.push('message',role='user',text='Не знаю, какой средний?')
        s.push('message',role='agent',text='Хотите, покажу рыночные ориентиры по трёшкам в центре Москвы?')
        restored=session.restore_session(s.id,'',BudgetModel())
        self.assertEqual(restored.history,[])
        await restored.receive_text('Да',command_id='after-restart')
        review=restored.request_review
        self.assertEqual(review['requested_action'],'market_prices')
        self.assertEqual(review['changes'],{})
        self.assertTrue(any('Хотите, покажу' in row['text'] for row in restored.llm.reviews[-1]['recent_dialogue']))
        self.assertIsNone(restored.profile['budget_max']);self.assertEqual(restored.call_targets,[])

    async def test_data_request_applies_its_new_conditions_before_choosing_the_tool(self):
        class CompositeModel(ScriptedLLM):
            async def chat(self,messages,**kw):
                if kw.get('tag')=='request_review':
                    return LLMResponse('',[ToolCall('request_information',{'action':'market_prices','update_conditions':True,
                        'research':{'respect_budget':False,'group_by':None,'aggregates':[]}})])
                if kw.get('tag')=='profile_extract':
                    return say(json.dumps({'fields':[
                        {'key':'city','value':'Казань','source':'Казани'},
                        {'key':'rooms_options','value':'[2]','source':'двушку'},
                        {'key':'housing','value':'both','source':'оба рынка'},
                        {'key':'payment','value':'cash','source':'наличными'}],
                        'criteria':[],'places':[],'unsupported':[],'selection':[]},ensure_ascii=False))
                return await super().chat(messages,**kw)
        s=self.agent(CompositeModel())
        await s.receive_text('Хочу двушку в Казани, оба рынка, наличными. Сколько такие обычно стоят?')
        await understanding.ensure(s)
        self.assertEqual(s.profile['city'],'Казань');self.assertEqual(s.profile['rooms_options'],[2])
        self.assertEqual(s.profile['housing'],'both');self.assertEqual(s.profile['payment'],'cash')
        self.assertEqual(s.requested_action,'market_prices');self.assertIsNone(s.profile['budget_max'])

    async def test_failed_requested_market_has_explicit_outcome_and_no_automatic_retry(self):
        s=self.agent();s.requested_action='market_prices';s.request_intent='question'
        with patch.object(sources,'market',AsyncMock(return_value={'error_kind':'insufficient_market_sample','count':2,'received':30})):
            result=await actions.execute(s,ToolCall('market_prices',{},'market-failed'))
        self.assertEqual(result['error_kind'],'insufficient_market_sample')
        self.assertIsNone(s.requested_action);self.assertIsNone(s.market)
        self.assertIn('только 2',result['error']);self.assertIn('хотя бы 6',result['error'])
        self.assertIsNone(s.profile['budget_max'])

    def test_information_cannot_smuggle_a_call_or_change_budget(self):
        from backend.agent import input_frame
        from backend.agent.request_schema import CHANGE_SCHEMA
        inputs=[{'text':'Да'}];tools=understanding.semantic_tools(input_frame.sparse_schema(CHANGE_SCHEMA,inputs),inputs,[])
        with self.assertRaises(ValueError):
            understanding.frame_from_call(ToolCall('request_information',{'action':'call_seller'}),tools,inputs)
        from backend.agent.capabilities import decision
        s=self.agent();self.assertFalse(decision(s,'affordable_budget')[0])
        s.requested_action='affordable_budget';self.assertTrue(decision(s,'affordable_budget')[0])

    def test_qa_sees_center_both_markets_history_and_measured_prices(self):
        s=self.agent();s.market=self.prices(s)
        s.push('message',role='agent',text='Средняя цена по этой выборке показана выше.')
        ctx=qa.build_context(s)
        self.assertTrue(ctx['запрос']['center_required']);self.assertEqual(ctx['запрос']['housing'],'both')
        self.assertEqual(ctx['рыночные ориентиры']['median'],40_000_000)
        self.assertTrue(ctx['последние реплики'])
        s.market=None;s.profile['housing']='new';s.market=self.prices(s)
        apply_request(s,{'housing':'secondary'})
        self.assertIsNone(s.market,'Смена рынка отзывает прежнюю оценку')

    async def test_price_request_survives_missing_prerequisites_without_authorizing_search(self):
        s=self.agent();s.profile=merge_profile({'city':'Москва'});s.request_intent='question';s.requested_action='market_prices'
        s.push('message',role='user',text='Три комнаты');s.accept_request('Три комнаты',s.seq)
        s.llm=ScriptedLLM();s.request_review={'seq':s.seq,'base_profile':deepcopy(s.profile),
            'intent':'refine','decision':'apply','changes':{'rooms_options':[3]},'question':'','options':[]}
        await understanding.ensure(s)
        self.assertEqual(s.requested_action,'market_prices')
        self.assertEqual(loop.next_search_action(s),'market_prices')
        self.assertFalse(s.red_lines_ok)

    def test_new_conditions_revoke_late_choices_and_allow_a_new_plan_after_reselection(self):
        from backend.agent.capabilities import decision
        from backend.agent import state
        for phase in ('price','chosen','completed'):
            with self.subTest(phase=phase):
                s=self.agent();s.profile=merge_profile({**s.profile,'budget_max':50_000_000})
                s.red_lines_ok=True;s.profile['_approved_budget_max']=50_000_000
                x=listing(id='old',city='Москва',rooms=3,price=40_000_000)
                s.search={'listings':[x],'pool':[x],'shown_ids':{'old'},'meta':{},'comps':[x]}
                s.shown=[x];s.calls={'old':{'price':39_000_000}};s.agreed={'old':39_000_000}
                s.chosen='old' if phase!='price' else None
                s.status={6:'done',7:'done',8:'done',9:'done'} if phase=='completed' else {6:'done',7:'done'}
                s.pending_decisions={'sign':{'kind':'sign','status':'waiting','decision_id':'stale','text':'Старый план','payload':{}}}
                if phase=='completed':s.push('done',status='completed',demo=True)
                s.push('message',role='agent',text='Прежняя квартира выбрана');seq=s.seq
                apply_request(s,{'city':'Казань','rooms_options':[2],'budget_max':12_000_000,'prefer_center':False,'center_required':False})
                self.assertIsNone(s.chosen);self.assertEqual(s.agreed,{});self.assertEqual(s.calls,{})
                self.assertFalse(s.red_lines_ok);self.assertFalse(s.resolve('sign',True,{},'stale'))
                self.assertFalse(decision(s,'prepare_deal')[0]);self.assertNotIn(8,s.status)
                restored=session.restore_session(s.id,'',ScriptedLLM())
                from backend.db import repo
                self.assertEqual(db.required(repo.get_chat,s.id).status,'running')
                self.assertIsNone(restored.chosen);self.assertEqual(restored.profile['city'],'Казань')
                self.assertTrue(any(e['type']=='purchase_reset' and e['seq']>seq for e in s.history))
                # Новое отдельное подтверждение и выбор: прежний done не блокирует новый сценарий.
                new=listing(id='new',city='Казань',rooms=2,price=10_000_000)
                restored.search={'listings':[new],'pool':[new],'shown_ids':{'new'},'meta':{},'comps':[new]}
                restored.red_lines_ok=True;restored.profile['_approved_budget_max']=12_000_000
                restored.shown=[new];restored.calls={'new':{'price':9_000_000}};restored.agreed={'new':9_000_000};restored.chosen='new'
                self.assertEqual(restored.products,{})
                self.assertTrue(decision(restored,'offer_products')[0])
                restored.products['choice']={'listing_id':'new'}
                self.assertTrue(decision(restored,'prepare_deal')[0])

    def test_changed_financing_keeps_chosen_property_but_revokes_completed_plan(self):
        s=self.agent();s.profile=merge_profile({**s.profile,'budget_max':50_000_000})
        s.chosen='chosen';s.agreed={'chosen':40_000_000};s.status={7:'done',8:'done',9:'done'}
        s.mortgage={'old':'calculation'}
        apply_request(s,{'payment':'mortgage_ready'})
        self.assertEqual(s.chosen,'chosen');self.assertEqual(s.agreed,{'chosen':40_000_000})
        self.assertIsNone(s.mortgage);self.assertNotIn(8,s.status);self.assertNotIn(9,s.status)


class MarketScope(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def test_explicit_client_money_overrides_an_unrelated_suggestion_but_ambiguous_quotes_are_rejected(self):
        from backend.agent.numbers import ground_money
        from backend.agent import profile_patch,input_frame
        from backend.agent.request_schema import CHANGE_SCHEMA
        text='Тогда лимит 80 миллионов рублей. Остальные условия сохрани.'
        inputs=[{'text':text}]
        for quote in (text,'80','80 миллионов'):
            result=profile_patch.decode({'fields':[{'key':'budget_max','value':'27000000','source':quote}],
                'criteria':[],'places':[],'unsupported':[],'selection':[]},profile_patch.schema(input_frame.schema(CHANGE_SCHEMA,inputs)),inputs)
            self.assertEqual(result['fields']['budget_max']['value'],80_000_000)
        text='Лимит 80 млн, первоначальный взнос 20 млн.'
        with self.assertRaises(ValueError):ground_money(27_000_000,text,[{'text':text}])
        self.assertEqual(ground_money(80_000_000,text,[{'text':text}]),80_000_000)
        self.assertEqual(ground_money(20_000_000,'20 млн',[{'text':text}]),20_000_000)
        self.assertEqual(ground_money(6_700_000,'6 700000',[{'text':'Лимит 6 700000'}]),6_700_000)

    async def test_both_markets_all_rooms_center_and_unknowns_share_one_sample_policy(self):
        p=merge_profile({'city':'Москва','rooms_options':[2,3],'housing':'both','prefer_center':True,'center_required':True})
        original=deepcopy(p)
        async def fetch(city,rooms,budget,page,okrug,new):
            self.assertIsNone(budget)
            rows=[listing(id=f'{rooms}-{new}-{i}',city=city,rooms=rooms,is_new=new,lat=55.755,lng=37.62,
                          price=(30_000_000 if new else 20_000_000)+i*100_000) for i in range(6)]
            rows+=[listing(id=f'far-{rooms}-{new}',city=city,rooms=rooms,is_new=new,lat=55.4,lng=37.3,price=1_000_000)]
            rows+=[listing(id=f'unknown-{rooms}-{new}',city=city,rooms=rooms,is_new=new,lat=None,lng=None,price=40_000_000)]
            return rows,{'status':'live'}
        with patch.object(sources.cian,'search',AsyncMock(side_effect=fetch)) as request:
            r=await sources.market(p)
        self.assertEqual(request.await_count,4);self.assertEqual(r['count'],24)
        self.assertEqual(r['unverified'],4);self.assertEqual([x['count'] for x in r['markets']],[12,12])
        self.assertGreater(r['median'],20_000_000);self.assertLess(r['median'],31_000_000)
        self.assertEqual(p,original);self.assertFalse(r['scope']['whole_market']);self.assertFalse(r['scope']['budget_applied'])

    async def test_unavailable_market_and_missing_coordinates_never_become_confirmed_center(self):
        p=merge_profile({'city':'Москва','rooms':3,'housing':'both','center_required':True})
        async def fetch(city,rooms,budget,page,okrug,new):
            if new:raise sources.SourceUnavailable('fixture unavailable')
            return [listing(id=f'no-coordinates-{i}',city=city,rooms=rooms,lat=None,lng=None) for i in range(8)],{'status':'cached'}
        with patch.object(sources.cian,'search',AsyncMock(side_effect=fetch)):
            result=await sources.market(p)
        self.assertEqual(result['count'],0);self.assertEqual(result['error_kind'],'insufficient_market_sample')
        self.assertNotIn('median',result)
        self.assertTrue({'cached_data','incomplete_markets','unverified_conditions'}<=set(result['reasons']))

    def test_market_cache_ignores_budget_but_rechecks_changed_location_and_market(self):
        p=merge_profile({'city':'Москва','rooms':3,'housing':'both','center_required':True})
        value={'profile_key':market.key(p),'generated_at':datetime.now(timezone.utc).isoformat()}
        self.assertTrue(market.current(value,{**p,'budget_max':50_000_000,'payment':'cash'}))
        self.assertFalse(market.current(value,{**p,'housing':'new'}))
        self.assertFalse(market.current(value,{**p,'center_required':False}))
