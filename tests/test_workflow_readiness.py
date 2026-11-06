"""Порядок решений, срок согласия на лимит и идемпотентность финансового расчёта."""
import unittest
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import actions, session
from backend.agent.actions import request as R, money, deal
from backend.agent.profile import merge_profile
from tests.fakes import ScriptedLLM, drive, say
from backend.llm import ToolCall
from backend.agent import validation
from tests.test_llm_and_agent import listing


class WorkflowReadinessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')

    def agent(self):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':9_000_000,'payment':'cash'})
        return s

    async def test_limits_are_confirmed_before_first_search_and_only_once(self):
        s=self.agent()
        s.query='Казань, двушка до 9 млн, наличными'
        s.llm=ScriptedLLM(say('Начинаю поиск.'))
        with patch.object(sources,'collect',AsyncMock(return_value=([listing()],[]))),patch.object(sources,'more',AsyncMock(return_value=[])):
            events=await drive(s,until=lambda e:e['type']=='confirm' and e['kind']=='selection')
        confirms=[e for e in events if e['type']=='confirm']
        self.assertEqual([e['kind'] for e in confirms],['red_lines','selection'])
        self.assertLess(confirms[0]['seq'],next(e['seq'] for e in events if e['type']=='search_progress' and e['on']))
        self.assertNotIn('confirm_red_lines',[t.name for t in actions.tools(s)])

    async def test_limit_survives_other_preferences_and_restart_but_budget_change_revokes_it(self):
        s=self.agent()
        s.ask=AsyncMock(return_value={'approved':True,'data':{}})
        await R.confirm_red_lines(s)
        R.apply_request(s,{'city':'Москва','rooms_options':[1,2],'prefer_center':True})
        self.assertTrue(s.red_lines_ok)
        await R.confirm_red_lines(s)
        s.ask.assert_awaited_once()
        s.remember()
        restored=session.restore_session(s.id,'Продолжить',s.llm)
        self.assertTrue(restored.red_lines_ok)
        R.apply_request(restored,{'budget_max':8_000_000})
        self.assertFalse(restored.red_lines_ok)
        restored.remember()
        self.assertFalse(session.restore_session(s.id,'Продолжить',s.llm).red_lines_ok)

    async def test_payment_is_not_required_for_reading_but_call_keeps_its_requirements(self):
        s=self.agent();s.red_lines_ok=True;s.profile['payment']=None;s.search={}
        self.assertTrue(actions.available(s,'search_listings'))
        self.assertFalse(actions.available(s,'call_seller'))
        with self.assertRaisesRegex(actions.ActionError,'ask_payment'):
            await deal.call_seller(s,'missing')

    async def test_refinement_requires_showing_updated_selection_before_calling(self):
        s=self.agent();s.red_lines_ok=True;s.prefs.sync(s.profile)
        x=listing()
        s.shown=[x]
        s.search={'listings':[x],'pool':[x],'shown_ids':{x['id']}}
        self.assertFalse(actions.available(s,'call_seller'))
        s.call_targets=[x['id']]  # отдельная просьба звонить, не просто наличие выдачи
        self.assertTrue(actions.available(s,'call_seller'))
        R.apply_request(s,{'kitchen_min':9})
        self.assertFalse(actions.available(s,'call_seller'))
        with patch.object(sources,'verify',AsyncMock()) as verify:
            with self.assertRaisesRegex(actions.ActionError,'show_selection'):
                await deal.call_seller(s,x['id'])
            s.shown=[x]
            with self.assertRaisesRegex(actions.ActionError,'текущей подборки'):
                await deal.call_seller(s,'another-listing')
            verify.assert_not_awaited()

    async def test_planner_cannot_write_profile_without_accepted_user_input(self):
        s=self.agent()
        before=dict(s.profile)
        result=await actions.execute(s,ToolCall('update_request',{'budget_max':100_000_000}))
        self.assertIn('error',result)
        self.assertEqual(s.profile,before)

    async def test_repeated_cash_calculation_has_no_repeated_message_or_panel(self):
        s=self.agent()
        await money.mortgage_programs(s)
        again=await money.mortgage_programs(s)
        self.assertTrue(again['already_calculated'])
        self.assertFalse(actions.available(s,'mortgage_programs'))
        self.assertEqual(len([e for e in s.history if e['type']=='message']),1)
        self.assertEqual(len([e for e in s.history if e['type']=='result' and e.get('kind')=='finance']),1)
        R.apply_request(s,{'features':['balcony']})
        self.assertFalse(actions.available(s,'mortgage_programs'))
        R.apply_request(s,{'city':'Москва'})
        self.assertFalse(actions.available(s,'mortgage_programs'))
        s.remember()
        restored=session.restore_session(s.id,'Продолжить',s.llm)
        self.assertTrue(money.programs_current(restored))
        R.apply_request(s,{'budget_max':10_000_000})
        self.assertTrue(actions.available(s,'mortgage_programs'))
        await money.mortgage_programs(s)
        self.assertEqual(len([e for e in s.history if e['type']=='message']),2)

    async def test_legacy_confirmation_is_restored_from_trusted_tool_results(self):
        s=self.agent()
        s.messages=[{'role':'assistant','content':'','tool_calls':[{'id':'approved','type':'function','function':{'name':'confirm_red_lines','arguments':'{}'}}]},
                    {'role':'tool','tool_call_id':'approved','content':'{"approved":true,"budget_max":9000000}'}]
        s.remember()
        self.assertTrue(session.restore_session(s.id,'Продолжить',s.llm).red_lines_ok)
        s.messages.extend([{'role':'assistant','content':'','tool_calls':[{'id':'changed','type':'function','function':{'name':'update_request','arguments':'{}'}}]},
                           {'role':'tool','tool_call_id':'changed','content':'{"profile":{"budget_max":10000000}}'}])
        s.remember()
        self.assertFalse(session.restore_session(s.id,'Продолжить',s.llm).red_lines_ok)

    async def test_cached_actions_still_validate_arguments(self):
        s=self.agent();s.red_lines_ok=True
        await money.mortgage_programs(s)
        for name in ('mortgage_programs', 'confirm_red_lines'):
            result=await actions.execute(s,ToolCall(name,{'unexpected':True}))
            self.assertIn('error',result)
        self.assertEqual(s.profile['budget_max'],9_000_000)
        self.assertEqual(len([e for e in s.history if e['type']=='message']),1)

    def test_declared_minimum_array_length_is_enforced(self):
        schema={'type':'array','minItems':1,'items':{'type':'string'}}
        with self.assertRaises(ValueError):validation.validate([],schema)
        validation.validate(['Москва'],schema)

    def test_profile_updates_require_new_client_input(self):
        s=self.agent();s.request_inputs=[{'seq':1,'text':'исходный запрос'}];s.request_review_seq=1
        self.assertNotIn('update_request',[t.name for t in actions.tools(s)])
        s.accept_request('Теперь бюджет десять миллионов',2)
        self.assertIn('update_request',[t.name for t in actions.tools(s)])
