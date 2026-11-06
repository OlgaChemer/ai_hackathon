"""Комбинации неполных запросов и исследование неизвестного свойства без анкеты."""
import itertools
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock,patch

from backend import db,sources,geo
from backend.agent import actions,session,understanding,capabilities,workflow,research
from backend.agent.actions.request import apply_request
from backend.agent.profile import merge_profile,passes_hard_filters
from backend.agent.criteria import evaluate
from backend.sources import market_estimate
from backend.llm import LLMResponse,ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class PartialRequests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self,profile):
        s=session.create_session('',ScriptedLLM());s.profile=merge_profile(profile)
        s.request_intent='refine';s.prefs.sync(s.profile)
        return s

    def test_generated_combinations_keep_only_named_constraints(self):
        for rooms,budget,floor,park,market in itertools.product((None,1,2),(None,20_000_000),(False,True),(False,True),(None,'new','secondary')):
            with self.subTest(rooms=rooms,budget=budget,floor=floor,park=park,market=market):
                p=merge_profile({'city':'Москва','rooms':rooms,'budget_max':budget,'housing':market,
                    'poi':['park'] if park else [],'filters':[{'field':'floor','op':'gt','values':['10'],'priority':'required'}] if floor else []})
                s=self.agent(p)
                self.assertTrue(capabilities.decision(s,'search_listings')[0])
                self.assertFalse(capabilities.decision(s,'call_seller')[0])
                for r,price,level,dist in itertools.product((1,2),(15_000_000,30_000_000),(10,11),(500,1500)):
                    x=listing(city='Москва',rooms=r,price=price,floor=level,is_new=market=='new',
                              nearby={'park':{'name':'Парк','dist':dist}})
                    expected=(rooms is None or r==rooms) and (budget is None or price<=budget) and (not floor or level>10) and (not park or dist<=1000)
                    actual=passes_hard_filters(x,p) and evaluate(p,x)['status']=='confirmed'
                    self.assertEqual(actual,expected)

    def test_explicit_unknown_and_unrestricted_clear_old_values_but_omission_preserves(self):
        s=self.agent({'city':'Москва','rooms':2,'budget_max':20_000_000})
        apply_request(s,{'poi':['park']});self.assertEqual(s.profile['rooms'],2)
        apply_request(s,{'field_states':{'rooms':'unknown','budget_max':'unrestricted'}})
        self.assertIsNone(s.profile['rooms']);self.assertIsNone(s.profile['budget_max'])
        self.assertEqual(s.profile['field_states']['rooms'],'unknown')
        self.assertEqual(s.profile['field_states']['budget_max'],'unrestricted')
        self.assertEqual(workflow.next_search_action(s),'search_listings')
        apply_request(s,{'rooms_options':[1,2],'budget_max':25_000_000})
        self.assertEqual(s.profile['rooms_options'],[1,2]);self.assertEqual(s.profile['field_states']['rooms'],'constrained')
        self.assertEqual(s.profile['field_states']['budget_max'],'constrained')

    def test_wildcard_url_does_not_invent_rooms_or_price(self):
        url=sources.cian.build_url('Москва',None,None)
        self.assertNotIn('room',url);self.assertNotIn('maxprice',url)
        self.assertIn('room2=1',sources.cian.build_url('Москва',2,None))
        self.assertEqual([q['rooms'] for q in sources.queries(merge_profile({'city':'Москва','rooms_options':[1,2]}))],[1,2])

    def test_financial_reading_needs_money_inputs_not_location_or_rooms(self):
        s=self.agent({'budget_max':20_000_000,'payment':'cash'})
        self.assertTrue(capabilities.decision(s,'mortgage_programs')[0])
        self.assertFalse(capabilities.decision(s,'search_listings')[0])
        s.profile['payment']='mortgage_saving'
        self.assertTrue(capabilities.decision(s,'savings_plan')[0])

    async def test_partial_search_executes_without_payment_or_agreements(self):
        s=self.agent({'city':'Москва','filters':[{'field':'floor','op':'gt','values':['10'],'priority':'required'}],'poi':['park']})
        rows=[listing(id=f'p{i}',city='Москва',floor=11+i,rooms=1+i%3,nearby={'park':{'name':'Парк','dist':400}}) for i in range(15)]
        with patch.object(sources,'collect',AsyncMock(return_value=(rows,[]))),patch.object(sources,'stored',return_value=[]),\
             patch.object(sources,'comparables',return_value=[]),patch.object(geo,'nearby',AsyncMock(return_value=True)):
            result=await actions.execute(s,ToolCall('search_listings',{}))
        self.assertNotIn('error',result);self.assertTrue(s.search['pool'])
        self.assertFalse(s.red_lines_ok);self.assertIsNone(s.profile['payment']);self.assertIsNone(s.profile['budget_max'])
        self.assertEqual(workflow.next_search_action(s),'show_selection')

    async def test_research_groups_unknown_rooms_within_budget_and_checks_park(self):
        s=self.agent({'city':'Москва','budget_max':20_000_000,'housing':'secondary','poi':['park']})
        s.research_query={'respect_budget':True,'group_by':'rooms','aggregates':[{'field':'price','function':'min'}]}
        rows=[listing(id=f'r{i}',city='Москва',rooms=1+i%2,price=10_000_000+i*500_000) for i in range(14)]
        async def nearby(items,kinds,**kwargs):
            for x in items:x['nearby']={'park':{'name':'Парк','dist':500}}
            return True
        with patch.object(sources.cian,'search',AsyncMock(return_value=(rows,{'status':'live'}))) as fetch,\
             patch.object(geo,'nearby',side_effect=nearby),patch.object(sources.store,'remember'):
            value=await market_estimate.estimate(research.profile(s))
        self.assertEqual(value['count'],14);self.assertEqual({g['value'] for g in value['analysis']['groups']},{1,2})
        self.assertTrue(value['scope']['budget_applied']);self.assertEqual(fetch.call_args.args[1],None)
        self.assertEqual(fetch.call_args.args[2],20_000_000)
        original=deepcopy(s.profile)
        with patch.object(sources,'market',AsyncMock(return_value=value)):
            result=await actions.execute(s,ToolCall('market_prices',{}))
        self.assertNotIn('error',result);self.assertEqual(s.profile,original)
        self.assertTrue(any('число комнат: 2' in e.get('text','') for e in s.history))

    async def test_short_answer_receives_question_context_and_uses_new_quote(self):
        class Model(ScriptedLLM):
            async def chat(model,messages,**kw):
                if kw.get('tag')=='request_review':return LLMResponse('',[ToolCall('profile_change',{})])
                if kw.get('tag')=='profile_extract':
                    data=json.loads(messages[-1]['content'])['data'];self.assertIn('Какой этаж',data['pending_decisions'][0]['text'])
                    self.assertEqual(data['messages'][-1]['text'],'Любой')
                    return LLMResponse(json.dumps({'fields':[],'criteria':[
                        {'key':'not_first_floor','priority':'remove','value':'','source':'Любой'},
                        {'key':'not_last_floor','priority':'remove','value':'','source':'Любой'}],
                        'places':[],'unsupported':[],'selection':[],'requested_card':[]}))
                return await super().chat(messages,**kw)
        s=self.agent({'city':'Москва','not_first_floor':True,'not_last_floor':True,'budget_max':20_000_000})
        s.llm=Model();s.pending_decisions={'clarify':{'kind':'clarify','text':'Какой этаж для вас предпочтительнее?','payload':{'chips':['Любой']},'status':'waiting'}}
        e=s.push('message',role='user',text='Любой');s.accept_request('Любой',e['seq'])
        r=await understanding.prepare(s)
        self.assertEqual(r['decision'],'apply');apply_request(s,r['changes'])
        self.assertFalse(s.profile['not_first_floor']);self.assertFalse(s.profile['not_last_floor'])
        self.assertEqual(s.profile['budget_max'],20_000_000)

    def test_research_query_reuses_numeric_executor_and_rejects_invalid_metrics(self):
        rows=[listing(id='a',area=40),listing(id='b',area=80)]
        result=research.execute(rows,{'respect_budget':True,'group_by':None,'aggregates':[{'field':'area','function':'max'}]})
        self.assertEqual(result['aggregates'][0]['value'],80)
        with self.assertRaises(ValueError):research.query({'respect_budget':True,'group_by':None,'aggregates':[{'field':'address','function':'max'}]})
