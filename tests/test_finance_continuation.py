"""Ипотечная анкета продолжается после выбора оплаты, в том числе перед звонком."""
import unittest
from unittest.mock import AsyncMock

from backend import db
from backend.agent import actions,capabilities,session,workflow
from backend.agent.profile import merge_profile
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class FinanceContinuation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self,intent='call',payment='mortgage_saving'):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':9_000_000,
            'payment':payment,'housing':'new'})
        s.profile['_extras_asked']=True;s.red_lines_ok=True
        s.request_intent=intent
        x=listing(id='chosen',is_new=True)
        s.search={'listings':[x],'pool':[x]};s.shown=[x];s.call_targets=['chosen']
        return s

    async def execute(self,s,name,approved,data):
        s.ask=AsyncMock(return_value={'approved':approved,'data':data})
        result=await actions.execute(s,ToolCall(name,{}))
        self.assertNotIn('error',result)
        return result

    async def test_saving_choice_requires_family_then_cash_then_programs_and_savings(self):
        s=self.agent()
        self.assertFalse(capabilities.decision(s,'call_seller')[0])
        self.assertEqual(workflow.next_search_action(s),'ask_family')
        await self.execute(s,'ask_family',True,{'children':1,'youngest_age':4,'matcap':737_200})
        self.assertEqual(workflow.next_search_action(s),'ask_deposit')
        await self.execute(s,'ask_deposit',True,{'deposit':2_000_000})
        self.assertEqual(workflow.next_search_action(s),'mortgage_programs')
        await self.execute(s,'mortgage_programs',True,{})
        self.assertEqual(s.mortgage['programs'][0]['down_payment'],2_737_200)
        self.assertEqual(workflow.next_search_action(s),'savings_plan')
        await self.execute(s,'savings_plan',True,{'monthly_income':250_000,'monthly_expenses':100_000,
            'savings':2_000_000,'horizon_months':12,'risk':'moderate'})
        self.assertTrue(capabilities.decision(s,'call_seller')[0])
        self.assertEqual(workflow.next_search_action(s),'call_seller')

    async def test_skip_keeps_search_available_and_does_not_reask_for_cash(self):
        s=self.agent(intent='refine',payment='mortgage_ready')
        s.selection_dirty=True
        self.assertEqual(workflow.next_search_action(s),'ask_family')
        await self.execute(s,'ask_family',True,{'children':0,'matcap':0})
        await self.execute(s,'ask_deposit',False,{})
        self.assertEqual(workflow.next_search_action(s),'mortgage_programs')
        await self.execute(s,'mortgage_programs',True,{})
        self.assertEqual(workflow.next_search_action(s),'show_selection')
        self.assertIsNone(s.mortgage)

    def test_cash_does_not_require_a_family_or_deposit_question(self):
        s=self.agent(payment='cash')
        self.assertEqual(workflow.next_search_action(s),'call_seller')
        self.assertFalse(capabilities.decision(s,'ask_family')[0])

    async def test_selected_flat_offers_its_price_without_restarting_unknown_budget_intake(self):
        from backend.agent.actions.request import confirm_red_lines
        s=self.agent();s.profile['budget_max']=None;s.red_lines_ok=False
        s.profile['field_states']['budget_max']='unknown'
        target=s.shown[0];target['price']=18_700_000
        self.assertEqual(workflow.next_search_action(s),'confirm_red_lines')
        self.assertFalse(capabilities.decision(s,'ask_client')[0])
        async def accept(kind,title,text,**kw):
            self.assertEqual(kind,'red_lines');self.assertIn('выбранной квартире',title)
            self.assertIn('18 700 000',text)
            self.assertIsNone(s.profile['budget_max'])  # цена не становится лимитом до решения
            self.assertEqual(kw['payload']['budget_max'],18_700_000)
            return {'approved':True,'data':{'budget_max':18_700_000,'budget_min':0}}
        s.ask=accept
        result=await confirm_red_lines(s)
        self.assertTrue(result['approved'])
        self.assertEqual(s.call_targets,['chosen']);self.assertIsNotNone(s.search)
        self.assertEqual(s.profile['budget_max'],18_700_000)
        self.assertTrue(s.red_lines_ok)
        self.assertEqual(workflow.next_search_action(s),'ask_family')

    async def test_rejecting_selected_price_does_not_create_purchase_authority(self):
        from backend.agent.actions.request import confirm_red_lines
        s=self.agent();s.profile['budget_max']=None;s.red_lines_ok=False
        s.ask=AsyncMock(return_value={'approved':False,'data':{}})
        await confirm_red_lines(s)
        self.assertIsNone(s.profile['budget_max']);self.assertFalse(s.red_lines_ok)
        self.assertEqual(s.request_intent,'hold')

    def test_initial_finance_form_does_not_claim_that_a_nonexistent_plan_was_reset(self):
        from backend.agent.actions.money import _set
        s=self.agent();s.status[2]='running'
        _set(s,children=0,matcap=0)
        self.assertFalse(any(e['type']=='purchase_reset' for e in s.history))
