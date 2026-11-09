"""Регрессии ревью PR10: деньги, конкретный объект звонка и изменение условий."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from backend import db
from backend.agent import actions,capabilities,guard,listing_query as Q,mortgage as M,session,state
from backend.agent.actions import request,search
from backend.agent.profile import merge_profile
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class PR10RegressionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self,**profile):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':15_000_000,
            'payment':'mortgage_ready','children':0,'deposit':3_000_000,'housing':'secondary',**profile})
        s.prefs.sync(s.profile)
        return s

    async def calculate(self,s,call_id='calculate'):
        result=await actions.execute(s,ToolCall('mortgage_programs',{},call_id))
        self.assertNotIn('error',result)
        return result

    async def test_cash_and_matcap_both_reduce_the_loan_without_rounding_away_money(self):
        s=self.agent(deposit=2_000_000,children=1,youngest_age=7,matcap=737_200)
        await self.calculate(s)
        market=s.mortgage['programs'][0]
        self.assertEqual(market['down_payment'],2_737_200)
        self.assertEqual(market['loan'],12_262_800)
        self.assertEqual(s.mortgage['matcap']['own_down'],2_000_000)

    async def test_zero_cash_never_creates_a_fictitious_down_payment(self):
        s=self.agent(deposit=0)
        await self.calculate(s)
        self.assertEqual(s.mortgage['programs'][0]['down_payment'],0)
        self.assertEqual(s.mortgage['programs'][0]['loan'],15_000_000)

    async def test_cash_above_the_price_does_not_force_an_unneeded_loan(self):
        s=self.agent(deposit=20_000_000)
        await self.calculate(s)
        self.assertEqual(s.mortgage['programs'][0]['loan'],0)
        self.assertEqual(s.mortgage['programs'][0]['monthly'],0)

    async def test_changed_cash_invalidates_programs_and_recalculates_the_payment(self):
        s=self.agent();first=await self.calculate(s,'first')
        s.pending_decisions['sign']={'status':'waiting','decision_id':'old-plan'}
        request.apply_request(s,{'deposit':6_000_000})
        self.assertIsNone(s.mortgage)
        self.assertNotIn('sign',s.pending_decisions)
        self.assertFalse(capabilities.programs_current(s))
        result=await self.calculate(s,'second')
        self.assertNotIn('already_calculated',result)
        self.assertLess(result['monthly'],first['monthly'])
        self.assertEqual(s.mortgage['programs'][0]['loan'],9_000_000)

    async def test_card_and_finance_panel_match_the_actual_program(self):
        s=self.agent(deposit=6_000_000);result=await self.calculate(s)
        pay=M.listing_payment(listing(city='Казань',price=15_000_000,is_new=False),s.profile)
        self.assertEqual(pay['monthly'],result['monthly'])
        self.assertEqual(pay['rate'],result['rate'])
        self.assertEqual(pay['down'],6_000_000)
        panel=next(e['data'] for e in reversed(s.history) if e.get('kind')=='finance')
        actual=panel['mortgage']['scenarios'][0]
        self.assertEqual(actual['loan'],9_000_000)
        self.assertEqual(actual['monthly'],pay['monthly'])
        self.assertEqual(panel['mortgage']['approved'],9_000_000)

    async def test_cash_and_filter_changed_together_also_clear_the_old_finance(self):
        s=self.agent();await self.calculate(s)
        x=listing(city='Казань',price=15_000_000,is_new=False)
        s.search={'listings':[x],'pool':[x],'shown_ids':{x['id']},'strict':False}
        request.apply_request(s,{'deposit':6_000_000,'apartments':'any'})
        self.assertIsNone(s.mortgage)
        self.assertIsNone(s.programs_result)
        self.assertIsNone(s.programs_key)
        await self.calculate(s,'changed-together')
        self.assertEqual(s.mortgage['programs'][0]['loan'],9_000_000)

    async def test_payment_cache_refreshes_without_recollecting_listings(self):
        s=self.agent();x=listing(city='Казань',price=15_000_000,is_new=False)
        meta={};search.score_into([x],[x],s.profile,meta)
        before=meta[x['id']]['pay']['monthly']
        request.apply_request(s,{'deposit':6_000_000})
        search.score_into([x],[x],s.profile,meta)
        self.assertLess(meta[x['id']]['pay']['monthly'],before)
        self.assertEqual(meta[x['id']]['pay']['down'],6_000_000)

    async def test_plan_uses_cash_and_the_agreed_price_instead_of_budget_twenty_percent(self):
        s=self.agent(deposit=6_000_000);await self.calculate(s)
        x=listing(city='Казань',price=15_000_000,is_new=False)
        s.search={'listings':[x]};s.chosen=x['id'];s.agreed[x['id']]=14_000_000;s.red_lines_ok=True
        s.ask=AsyncMock(return_value={'approved':False,'data':{}})
        result=await actions.execute(s,ToolCall('prepare_deal',{'listing_id':x['id']},'deal'))
        self.assertNotIn('error',result)
        plan=next(e['data'] for e in s.history if e.get('kind')=='deal')
        self.assertEqual(plan['loan'],8_000_000)
        self.assertEqual(plan['down_payment'],6_000_000)
        self.assertEqual(plan['monthly'],M.listing_payment(x,s.profile,price=14_000_000)['monthly'])

    async def test_skip_is_persisted_and_defers_finance_without_reasking(self):
        s=self.agent(deposit=None)
        s.ask=AsyncMock(return_value={'approved':False,'data':{}})
        result=await actions.execute(s,ToolCall('ask_deposit',{},'skip'))
        self.assertTrue(result['skipped'])
        restored=self.agent();state.restore(restored,state.snapshot(s))
        self.assertFalse(capabilities.decision(restored,'ask_deposit')[0])
        result=await self.calculate(restored)
        self.assertEqual(result['mode'],'needs_deposit')
        self.assertIsNone(restored.profile['deposit'])
        self.assertIsNone(restored.mortgage)
        self.assertIsNone(M.listing_payment(listing(price=15_000_000),restored.profile))

    async def test_cash_given_after_skip_resumes_calculation(self):
        s=self.agent(deposit=None);s.profile['_deposit_skipped']=True
        await self.calculate(s,'skipped-info')
        request.apply_request(s,{'deposit':3_000_000})
        self.assertNotIn('_deposit_skipped',s.profile)
        result=await self.calculate(s,'known-cash')
        self.assertIn('monthly',result)
        self.assertEqual(s.mortgage['programs'][0]['loan'],12_000_000)

    def test_explicit_request_can_reopen_the_skipped_card(self):
        s=self.agent(deposit=None);s.profile['_deposit_skipped']=True
        s.requested_action='ask_deposit'
        self.assertTrue(capabilities.decision(s,'ask_deposit')[0])

    def test_family_program_does_not_claim_a_missing_minimum_down_payment(self):
        result=M.programs(7_000_000,'Казань',{'children':1,'youngest_age':4},
                          is_secondary=False,down_payment=700_000)
        family=next(c for c in result['programs'] if c['key']=='family')
        self.assertEqual(family['status'],'no')
        self.assertEqual(family['needs_down'],1_400_000)

    def selection(self):
        s=self.agent(payment='cash');a=listing(id='first',city='Казань');b=listing(id='second',city='Казань')
        s.shown=[a,b];s.search={'pool':[a,b],'listings':[a,b]};s.prefs.liked=['first','second']
        return s

    async def test_specific_call_consent_preserves_other_favorites(self):
        s=self.selection()
        s.pending['selection']=asyncio.get_running_loop().create_future()
        s.pending_ids['selection']='choose'
        self.assertTrue(s.resolve('selection',True,{'action':'proceed','listing_id':'second',
            'liked':['first','second']},'choose'))
        s.ask=AsyncMock(return_value=s.pending['selection'].result())
        await search.wait_selection(s,Q.default_query(),{'remaining':0,'aggregates':[],'groups':[]})
        self.assertEqual(s.call_targets,['second'])
        self.assertEqual(s.prefs.liked,['first','second'])

    async def test_bulk_consent_still_uses_all_liked_listings(self):
        s=self.selection();s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'proceed'}})
        await search.wait_selection(s,Q.default_query(),{'remaining':0,'aggregates':[],'groups':[]})
        self.assertEqual(s.call_targets,['first','second'])

    async def test_forged_or_stale_specific_target_keeps_the_decision_open(self):
        for lid,stale in [('unseen',False),('second',True)]:
            s=self.selection();s.selection_profile_stale=stale
            future=asyncio.get_running_loop().create_future();s.pending['selection']=future
            s.pending_ids['selection']='choose'
            with self.assertRaises(guard.InputRejected):
                s.resolve('selection',True,{'action':'proceed','listing_id':lid},'choose')
            self.assertFalse(future.done());self.assertEqual(s.call_targets,[])

    def test_apartment_filter_removal_restores_candidates_from_the_original_pool(self):
        s=self.agent(apartments='hide');a=listing(id='flat',city='Казань',is_apartments=False)
        b=listing(id='apartment',city='Казань',is_apartments=True)
        s.search={'pool':[a],'listings':[a,b],'shown_ids':{'flat'},'strict':False}
        request.apply_request(s,{'apartments':'any'})
        self.assertEqual({x['id'] for x in s.search['pool']},{'flat','apartment'})
        self.assertEqual(s.search['shown_ids'],set())

    async def test_confirmation_normalizes_legacy_zero_to_the_thirty_percent_corridor(self):
        s=self.agent(budget_min=0)
        s.ask=AsyncMock(return_value={'approved':True,'data':{'budget_min':0,'budget_max':15_000_000}})
        await request.confirm_red_lines(s)
        self.assertEqual(s.profile['budget_min'],10_500_000)
        request.apply_request(s,{'budget_max':20_000_000,'budget_min':0})
        self.assertEqual(s.profile['budget_min'],0)
