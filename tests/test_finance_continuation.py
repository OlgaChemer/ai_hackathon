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
