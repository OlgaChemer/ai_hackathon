"""Консультация перед подбором: реальные ориентиры, адресный ответ и восстановление."""
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock,patch

from backend import db,sources
from backend.agent import actions,capabilities,clarification,guidance,session,workflow
from backend.agent.actions.request import apply_request
from backend.agent.profile import merge_profile
from backend.llm import ToolCall
from backend.sources import market_estimate
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class GuidedIntake(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self,**profile):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Тюмень','prefer_center':True,**profile})
        s.request_intent='search';s.request_review_seq=1;s.prefs.sync(s.profile)
        return s

    def test_city_only_asks_budget_first_and_blocks_search_or_extra_questionnaire(self):
        s=self.agent()
        self.assertEqual(workflow.next_search_action(s),'ask_client')
        self.assertEqual(workflow.planned_call(s,'ask_client').arguments['field'],'budget_max')
        for name in ('search_listings','ask_payment','ask_family','ask_housing','market_prices'):
            self.assertFalse(capabilities.decision(s,name)[0],name)
        self.assertTrue(capabilities.argument_denial(s,'ask_client',{'question':'Рынок?','field':'housing'}))

    def test_named_budget_asks_only_rooms_and_explicit_broad_choice_allows_search(self):
        s=self.agent(budget_max=12_000_000)
        self.assertEqual(workflow.planned_call(s,workflow.next_search_action(s)).arguments['field'],'rooms')
        apply_request(s,{'field_states':{'rooms':'unrestricted'}})
        self.assertEqual(workflow.next_search_action(s),'search_listings')
        self.assertEqual(s.profile['budget_max'],12_000_000)

    async def help(self,s):
        from backend.agent.pricing import CITY_CENTERS
        lat,lng=CITY_CENTERS[s.profile['city']]
        rows=[listing(id=f'help-{i}',city=s.profile['city'],rooms=1+i%3,price=4_000_000+i*100_000,lat=lat,lng=lng) for i in range(18)]
        with patch.object(sources.cian,'search',AsyncMock(return_value=(rows,{'status':'live'}))),\
             patch.object(sources.store,'remember'):
            value=await market_estimate.estimate({**s.profile,'_research_query':guidance.research_query(s)})
        before=deepcopy(s.profile)
        with patch.object(sources,'market',AsyncMock(return_value=value)) as source:
            result=await actions.execute(s,ToolCall('market_prices',{}))
        self.assertNotIn('error',result)
        self.assertEqual(s.profile,before)
        self.assertEqual(source.await_count,1)
        return value

    async def test_unknown_budget_shows_real_prices_then_asks_rooms_without_inventing_money(self):
        s=self.agent(city='Москва',field_states={'budget_max':'unknown'})
        self.assertEqual(workflow.next_search_action(s),'market_prices')
        m=await self.help(s)
        self.assertTrue(m['analysis']['groups'])
        self.assertFalse(m['scope']['budget_applied'])
        self.assertEqual(guidance.question(s)['field'],'budget_max')
        self.assertIn('С учётом этих ориентиров',guidance.question(s)['question'])
        self.assertTrue(any('медиана' in e.get('text','') for e in s.history))
        s.request_review_seq+=1  # клиент остаётся неопределившимся после помощи
        self.assertEqual(guidance.question(s)['field'],'rooms')
        self.assertIsNone(s.profile['budget_max'])

    async def test_unknown_rooms_researches_with_known_budget_then_allows_examples(self):
        s=self.agent(city='Москва',budget_max=12_000_000,field_states={'rooms':'unknown'})
        m=await self.help(s)
        self.assertTrue(m['scope']['budget_applied'])
        self.assertEqual(guidance.question(s)['field'],'rooms')
        s.request_review_seq+=1
        self.assertEqual(workflow.next_search_action(s),'search_listings')
        self.assertEqual(s.profile['budget_max'],12_000_000)
        self.assertIsNone(s.profile['rooms'])
        s.remember()
        restored=session.restore_session(s.id,'',s.llm)
        self.assertEqual(restored.guidance,s.guidance)
        self.assertTrue(guidance.search_ready(restored))

    async def test_source_failure_explains_and_does_not_loop_or_invent_prices(self):
        s=self.agent(rooms=2,field_states={'budget_max':'unknown'})
        with patch.object(sources,'market',AsyncMock(return_value=None)) as source:
            result=await actions.execute(s,ToolCall('market_prices',{}))
        self.assertIn('error_kind',result)
        self.assertEqual(source.await_count,1)
        self.assertTrue(any('Циан сейчас не вернул' in e.get('text','') for e in s.history))
        self.assertEqual(workflow.next_search_action(s),'ask_client')
        s.request_review_seq+=1
        self.assertEqual(workflow.next_search_action(s),'search_listings')
        self.assertIsNone(s.profile['budget_max'])

    def test_explicit_information_request_is_allowed_before_intake(self):
        s=self.agent(budget_max=20_000_000)
        s.requested_action='market_prices';s.research_query={'respect_budget':True,'group_by':'rooms','aggregates':[]}
        self.assertEqual(workflow.next_search_action(s),'market_prices')

    async def test_requested_research_finishes_with_help_question_instead_of_more_tools(self):
        s=self.agent(city='Москва',budget_max=20_000_000)
        s.requested_action='market_prices';s.request_intent='question'
        s.research_query=guidance.research_query(s)
        await self.help(s)
        self.assertIsNone(s.requested_action)
        self.assertEqual(workflow.next_search_action(s),'ask_client')
        self.assertTrue(capabilities.decision(s,'ask_client')[0])
        self.assertEqual(workflow.planned_call(s,'ask_client').arguments['field'],'rooms')

    async def test_selection_count_and_order_are_understood_before_first_search(self):
        import json
        from backend.agent import understanding
        from backend.llm import LLMResponse
        s=self.agent(city='Москва',budget_max=20_000_000)
        args={'operation':'rank','limit':3,'order_by':'area','direction':'desc','keep_order':False,
              'scope':'all','group_by':None,'aggregates':[],
              'update_conditions':False,'count_source':'три','scope_source':None}
        async def chat(messages,**kw):
            if kw.get('tag')=='profile_extract':
                return LLMResponse(json.dumps({key:[] for key in ('fields','criteria','states','filters','places','unsupported','requested_card','withdraw_unsupported')}))
            self.assertIn('read_selection',[t.name for t in kw['tools']])
            return LLMResponse('',[ToolCall('read_selection',args)])
        s.llm.chat=chat
        s.request_review_seq=0
        text='Покажи три самых просторных предложения в этом бюджете.'
        e=s.push('message',role='user',text=text);s.accept_request(text,e['seq'])
        r=await understanding.prepare(s)
        self.assertEqual(r['selection']['limit'],3)
        self.assertEqual(r['selection']['sort'],[{'field':'area','direction':'desc'}])
        self.assertEqual(r['changes'],{})

    def test_current_selection_is_not_interviewed_again_and_new_city_changes_help_scope(self):
        s=self.agent()
        s.shown=[listing()];s.search={'pool':s.shown,'listings':s.shown}
        self.assertTrue(guidance.search_ready(s))
        s.search=None;s.shown=[]
        s.guidance={'assisted':{'budget_max':guidance.scope(s,'budget_max'),'rooms':guidance.scope(s,'rooms')}}
        self.assertTrue(guidance.search_ready(s))
        apply_request(s,{'city':'Москва'})
        self.assertFalse(guidance.search_ready(s))

    def test_market_ignorance_does_not_withdraw_personal_budget(self):
        self.assertIsNone(clarification.ground_state('budget_max','unknown','Я не знаю цены, помоги понять где могу купить'))
        self.assertEqual(clarification.ground_state('budget_max','unknown','Бюджет пока не знаю'),'unknown')
        self.assertEqual(clarification.ground_state('budget_max','unknown','Пока не знаю',bound_field='budget_max'),'unknown')
        self.assertEqual(clarification.ground_state('budget_max','unknown','Цена не важна'),'unrestricted')
