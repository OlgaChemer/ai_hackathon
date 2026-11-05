import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.db import repo
from backend.agent import actions, persistence, session
from backend.agent.actions.request import updated_profile
from backend.agent.profile import merge_profile
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing
from backend.agent import commands, criteria, conditions, loop
from backend.agent import guard
from backend.agent.actions import deal
from backend.agent.pricing import fair_price
from backend.llm import LLMResponse


class DurableStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':9_000_000,'payment':'cash'})
        s.profile['_approved_budget_max']=s.profile['budget_max'];s.red_lines_ok=True
        s.prefs.sync(s.profile)
        return s

    def with_listing(self):
        s=self.agent();x=listing()
        s.search={'listings':[x],'pool':[x],'shown_ids':{x['id']},'meta':{},'comps':[x]}
        s.shown=[x];s.call_targets=[x['id']]
        s.calls={x['id']:{'status':'agreed','price':7_000_000,'viewing':'завтра','slots':['завтра'],'learned':{}}}
        s.agreed={x['id']:7_000_000};s.chosen=x['id']
        s.remember()
        return s,x

    async def wait_card(self,s,kind,task):
        for _ in range(100):
            if kind in s.pending:return
            if task.done():task.result();self.fail('Операция завершилась до карточки '+kind)
            await asyncio.sleep(.001)
        self.fail('Карточка не появилась: '+kind)

    async def cancel(self,task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_inflight_receipt_stays_accepted_and_retry_joins_original(self):
        s=self.agent();entered=asyncio.Event();release=asyncio.Event()
        async def receive(*args):
            s.push('message',role='user',text='команда')
            entered.set();await release.wait();return 'message'
        with patch.object(s,'_receive_text',receive):
            first=asyncio.create_task(s.receive_text('команда',command_id='retry'))
            await entered.wait()
            self.assertEqual([r['id'] for r in db.required(repo.queued_commands,s.id)],['retry'])
            second=asyncio.create_task(s.receive_text('команда',command_id='retry'))
            await asyncio.sleep(0);self.assertFalse(second.done())
            release.set();self.assertEqual(await first,await second)
        self.assertEqual(sum(e.get('text')=='команда' for e in s.history),1)
        self.assertEqual(db.required(repo.queued_commands,s.id),[])

    async def test_invalid_decision_is_rejected_durably_without_retry_on_restart(self):
        s=self.agent();task=asyncio.create_task(s.ask('payment','Оплата','Как?'))
        await self.wait_card(s,'payment',task)
        for _ in range(2):
            with self.assertRaises(guard.InputRejected):
                await s.receive_decision('payment',True,{'payment':'nonsense'},command_id='invalid')
        self.assertEqual(db.required(repo.queued_commands,s.id),[])
        restored=session.restore_session(s.id,'',s.llm)
        self.assertEqual(restored.pending_decisions['payment']['status'],'waiting')
        await self.cancel(task)

    async def test_completed_command_is_not_repeated_after_restart(self):
        s=self.agent();await s.receive_text('Продолжить',command_id='once')
        restored=session.restore_session(s.id,'',s.llm);seq=restored.seq
        self.assertEqual(await restored.receive_text('Продолжить',command_id='once'),'message')
        self.assertEqual(restored.seq,seq);self.assertTrue(restored.inbox.empty())

    async def test_restart_preserves_each_card_id_and_executes_its_original_operation(self):
        cases=[('confirm_red_lines','red_lines',{}),('ask_payment','payment',{}),
               ('ask_family','family',{}),('ask_housing','housing',{}),
               ('ask_client','clarify',{'question':'Что уточнить?'}),
               ('confirm_price','price',{'listing_id':'cian-1'}),
               ('plan_viewing','object',{'listing_id':'cian-1'}),
               ('prepare_deal','sign',{'listing_id':'cian-1'})]
        for name,kind,args in cases:
            with self.subTest(name=name):
                s,x=self.with_listing()
                if kind=='red_lines':s.red_lines_ok=False;s.profile.pop('_approved_budget_max',None)
                if kind=='payment':s.profile['payment']=None
                if kind=='family':s.profile['payment']='mortgage_ready';s.profile['children']=None
                s.remember()
                call=ToolCall(name,args,'restore-'+name)
                task=asyncio.create_task(actions.execute(s,call));await self.wait_card(s,kind,task)
                ticket=deepcopy(s.pending_decisions[kind]);await self.cancel(task)
                restored=session.restore_session(s.id,'',s.llm);restored._replaying_operation=True
                resumed=asyncio.create_task(actions.execute(restored,call))
                await self.wait_card(restored,kind,resumed)
                self.assertEqual(restored.pending_ids[kind],ticket['decision_id'])
                self.assertEqual(restored.pending_decisions[kind]['payload'],ticket['payload'])
                self.assertEqual(restored.history,[],'Повтор карточки/реплик при восстановлении')
                self.assertTrue(restored.resolve(kind,False,{},ticket['decision_id']))
                result=await resumed
                self.assertNotIn('error',result)
                self.assertIsNone(restored.active_operation)
                self.assertNotIn(kind,restored.pending_decisions)

    async def test_call_card_recovers_without_rechecking_or_automatically_dialling(self):
        s,x=self.with_listing();s.calls={};s.agreed={};s.chosen=None
        call=ToolCall('call_seller',{'listing_id':x['id']},'call-recovery')
        with patch.object(sources,'verify',AsyncMock(return_value={'alive':True})) as verify:
            task=asyncio.create_task(actions.execute(s,call));await self.wait_card(s,'call',task)
            ticket=deepcopy(s.pending_decisions['call']);await self.cancel(task)
            restored=session.restore_session(s.id,'',s.llm);restored._replaying_operation=True
            resumed=asyncio.create_task(actions.execute(restored,call));await self.wait_card(restored,'call',resumed)
            self.assertEqual(verify.await_count,1)
            self.assertEqual(restored.pending_ids['call'],ticket['decision_id'])
            restored.resolve('call',False,{},ticket['decision_id'])
            self.assertFalse((await resumed)['called'])
        self.assertFalse(any(e['type']=='call_start' for e in restored.history))

    async def test_internal_type_error_is_not_disguised_as_bad_model_arguments(self):
        s=self.agent();actions._load();entry=actions.REGISTRY['market_prices']
        from dataclasses import replace
        with patch.dict(actions.REGISTRY,market_prices=replace(entry,run=AsyncMock(side_effect=TypeError('implementation defect')))):
            with self.assertRaisesRegex(TypeError,'implementation defect'):
                await actions.execute(s,ToolCall('market_prices',{},'bad-code'))

    async def test_selection_restores_same_cards_and_consumes_likes_without_advancing(self):
        s,x=self.with_listing();s.calls={};s.agreed={};s.chosen=None;s.call_targets=[]
        call=ToolCall('show_selection',{},'selection-recovery')
        task=asyncio.create_task(actions.execute(s,call));await self.wait_card(s,'selection',task)
        shown=deepcopy(s.shown);ticket=deepcopy(s.pending_decisions['selection']);await self.cancel(task)
        restored=session.restore_session(s.id,'',s.llm);restored._replaying_operation=True
        resumed=asyncio.create_task(actions.execute(restored,call));await self.wait_card(restored,'selection',resumed)
        self.assertEqual(restored.shown,shown);self.assertEqual(restored.history,[])
        restored.resolve('selection',True,{'action':'more','liked':[x['id']]},ticket['decision_id'])
        await resumed
        self.assertEqual(restored.prefs.liked,[x['id']]);self.assertEqual(restored.selection_operation,'more')
        self.assertEqual(restored.call_targets,[])

    async def test_unknown_external_outcome_requires_review_without_resuming_call(self):
        s,x=self.with_listing();s.started=True
        s.active_operation={'id':'unknown-call','name':'call_seller','arguments':{'listing_id':x['id']},
                            'request_seq':0,'status':'external_started'}
        s.remember();restored=session.restore_session(s.id,'',s.llm)
        entered=asyncio.Event()
        async def think(*args,**kwargs):entered.set();await asyncio.Future()
        with patch.object(loop,'think',think),patch.object(deal.N,'run') as dial:
            task=asyncio.create_task(loop.run(restored));await entered.wait();await self.cancel(task)
        dial.assert_not_called()
        self.assertEqual(restored.operations['unknown-call']['status'],'needs_review')
        self.assertEqual(restored.call_targets,[])
        self.assertEqual(restored.last_issue['kind'],'external_outcome_unknown')

    async def test_saved_external_result_finishes_without_a_second_call(self):
        s,x=self.with_listing();s.calls={};s.agreed={};s.chosen=None
        fp=fair_price(x,[x]);call=ToolCall('call_seller',{'listing_id':x['id']},'finished-call')
        result={'status':'agreed','price':7_000_000,'viewing':'завтра','learned':{},'corrections':{},'within_authority':True}
        s.active_operation={'id':call.id,'name':call.name,'arguments':call.arguments,'request_seq':0,
            'status':'external_completed','call_mode':'auto','external_result':result,
            'call_context':{'listing':x,'fair':fp,'authority':7_500_000,'seller':{},'questions':[],
                            'slots':['завтра'],'page':{},'note':None,'legal':{},'authorization':'grant'}}
        s.remember();restored=session.restore_session(s.id,'',s.llm);restored._replaying_operation=True
        with patch.object(deal.N,'run') as dial:
            done=await actions.execute(restored,call)
        dial.assert_not_called();self.assertTrue(done['called'])
        self.assertEqual(restored.calls[x['id']]['price'],7_000_000)
        again=await actions.execute(restored,call)
        self.assertEqual(again,done)

    async def test_delayed_model_answer_is_discarded_after_new_message(self):
        s=self.agent();s.started=True;entered=asyncio.Event();release=asyncio.Event();calls=0
        async def think(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:entered.set();await release.wait();return LLMResponse('Устаревший ответ',[])
            return LLMResponse('Ответ на новое сообщение',[])
        with patch.object(loop,'think',think),patch.object(actions,'execute',AsyncMock(return_value={'reviewed':True})):
            task=asyncio.create_task(loop.run(s));await entered.wait()
            await s.receive_text('Новый вопрос',command_id='newest');release.set()
            for _ in range(100):
                if any(e.get('text')=='Ответ на новое сообщение' for e in s.history):break
                await asyncio.sleep(.001)
            await self.cancel(task)
        self.assertFalse(any(e.get('text')=='Устаревший ответ' for e in s.history))
        self.assertTrue(any(e.get('text')=='Ответ на новое сообщение' for e in s.history))

    def test_equivalent_conditions_and_removal_have_one_unknown_policy(self):
        for named,field,value,op in [('area_min','area',50,'gte'),('year_min','year',2027,'gte'),
                                     ('metro_max_minutes','metro_minutes',10,'lte')]:
            with self.subTest(field=field):
                legacy=merge_profile({named:value})
                generic=merge_profile({'filters':[{'field':field,'op':op,'values':[str(value)],'priority':'required'}]})
                x=listing(area=None,year=None,metro=None)
                a,b=criteria.evaluate(legacy,x),criteria.evaluate(generic,x)
                self.assertEqual(a['status'],b['status']);self.assertEqual(a['status'],'unverified')
                changed=updated_profile(legacy,{'filters':generic['filters']})
                self.assertEqual(len(conditions.compile_profile(changed)),1)
                removed=updated_profile(changed,{'clear_criteria':[named]})
                self.assertEqual(conditions.compile_profile(removed),[])

    def test_clearing_all_nearby_requirements_also_clears_generic_aliases(self):
        p=merge_profile({'poi':['school'],'filters':[{'field':'nearby:clinic','op':'lte','values':['500'],'priority':'required'}]})
        self.assertEqual(conditions.compile_profile(updated_profile(p,{'poi':[]})),[])

    def test_snapshot_restores_the_business_process_not_just_the_screen(self):
        s=self.agent();x=listing()
        s.search={'listings':[x],'pool':[x],'shown_ids':{x['id']},'meta':{},'comps':[x]}
        s.shown=[x];s.prefs.liked=[x['id']];s.prefs.districts_plus={'район'}
        s.calls={x['id']:{'status':'agreed','price':8_000_000,'viewing':'завтра','slots':['завтра'],'learned':{}}}
        s.agreed={x['id']:8_000_000};s.chosen=x['id']
        s.remember()
        restored=session.restore_session(s.id,'',s.llm)
        self.assertEqual(restored.calls,s.calls)
        self.assertEqual(restored.agreed,s.agreed)
        self.assertEqual(restored.chosen,s.chosen)
        self.assertEqual(restored.search['shown_ids'],{x['id']})
        self.assertEqual(restored.prefs.districts_plus,{'район'})
        self.assertEqual(restored.prefs.liked,[x['id']])

    def test_failed_transaction_never_publishes_an_event_or_partial_state(self):
        s=self.agent();s.remember();before=deepcopy(s.profile)
        queue=asyncio.Queue();s.subscribers.add(queue);seq=s.seq
        with patch.object(repo,'commit_dialogue',side_effect=RuntimeError('disk failure')):
            with self.assertRaises(db.PersistenceError):
                with persistence.atomic(s):
                    s.profile['budget_max']=30_000_000
                    s.push('result',kind='profile',data=s.profile)
        self.assertEqual(s.seq,seq);self.assertTrue(queue.empty())
        self.assertEqual(s.profile,before)
        self.assertEqual(db.required(repo.get_chat,s.id).profile,before)

    def test_stale_owner_is_rejected_without_delivery(self):
        first=self.agent();first.remember()
        stale=session.restore_session(first.id,'',first.llm)
        first.push('message',role='agent',text='первая версия')
        queue=asyncio.Queue();stale.subscribers.add(queue)
        with self.assertRaises(db.StateConflict):stale.push('message',role='agent',text='устаревшая версия')
        self.assertTrue(queue.empty())
        self.assertNotIn('устаревшая',str(db.required(repo.chat_events,first.id)))

    async def test_accepted_decision_survives_crash_before_its_continuation(self):
        s=self.agent()
        task=asyncio.create_task(s.ask('price','Цена','Подтвердить?',price=8_000_000))
        await asyncio.sleep(0)
        decision=s.pending_ids['price']
        self.assertTrue(s.resolve('price',True,{},decision))
        restored=session.restore_session(s.id,'',s.llm)
        self.assertEqual(restored.pending_decisions['price']['decision_id'],decision)
        answer=await restored.ask('price','Другая цена','Не использовать этот текст',price=1)
        self.assertTrue(answer['approved'])
        self.assertEqual(restored.pending_decisions['price']['payload']['price'],8_000_000)
        await task

    async def test_command_duplicate_has_one_message_and_one_effect(self):
        s=self.agent()
        self.assertEqual(await s.receive_text('Продолжить',command_id='same'),'message')
        seq=s.seq
        self.assertEqual(await s.receive_text('Продолжить',command_id='same'),'message')
        self.assertEqual(s.seq,seq)
        self.assertEqual(s.inbox.qsize(),1)
        with self.assertRaises(db.StateConflict):await s.receive_text('Другое сообщение',command_id='same')

    async def test_slow_search_cannot_publish_a_result_of_an_old_request(self):
        s=self.agent();s.request_intent='search'
        old=[listing(id='old')];s.shown=old
        entered=asyncio.Event();release=asyncio.Event()
        async def collect(*args,**kwargs):
            entered.set();await release.wait()
            return [listing(id='outdated')],[]
        with patch.object(sources,'collect',collect),patch.object(sources,'stored',return_value=[]):
            task=asyncio.create_task(actions.execute(s,ToolCall('search_listings',{},'slow')))
            await entered.wait()
            await s.receive_text('Изменяю пожелания',command_id='new-request')
            release.set()
            with self.assertRaises(session.Superseded):await task
        self.assertEqual(s.shown,old)
        self.assertIsNone(s.search)
        self.assertFalse(any(e['type']=='result' and e.get('kind')=='search' for e in s.history))

    async def test_two_questions_are_both_answered_and_the_card_stays_open(self):
        s=self.agent()
        card=asyncio.create_task(s.ask('selection','Выбор','Что выбираете?'))
        await asyncio.sleep(0)
        from backend.agent import understanding,qa
        async def review(s):return {'seq':s.request_inputs[-1]['seq'],'intent':'question','decision':'unchanged'}
        entered=asyncio.Event();release=asyncio.Event()
        async def reply(s,text):
            if text=='Первый вопрос':entered.set();await release.wait()
            return 'Ответ: '+text
        with patch.object(understanding,'prepare_from_http',review),patch.object(qa,'reply',reply):
            self.assertEqual(await s.receive_text('Первый вопрос',command_id='q1'),'question')
            await entered.wait()
            self.assertEqual(await s.receive_text('Второй вопрос',command_id='q2'),'question')
            release.set();await s.qa_task
        answers=[e for e in s.history if e.get('in_reply_to')]
        self.assertEqual([e['in_reply_to'] for e in answers],['q1','q2'])
        self.assertIn('selection',s.pending)
        card.cancel()
        with self.assertRaises(asyncio.CancelledError):await card
