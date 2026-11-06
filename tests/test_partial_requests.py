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
        named=merge_profile({'city':'Москва','center_required':True,'prefer_center':True})
        generic=merge_profile({'city':'Москва','prefer_center':True,'filters':[
            {'field':'center_km','op':'lte','values':['5'],'priority':'required'}]})
        self.assertEqual(market_estimate.key(named),market_estimate.key(generic))

    def test_financial_reading_needs_money_inputs_not_location_or_rooms(self):
        s=self.agent({'budget_max':20_000_000,'payment':'cash'})
        self.assertTrue(capabilities.decision(s,'mortgage_programs')[0])
        self.assertFalse(capabilities.decision(s,'search_listings')[0])
        s.profile['payment']='mortgage_saving'
        self.assertTrue(capabilities.decision(s,'savings_plan')[0])

    def test_explicit_call_executes_verified_target_and_does_not_repeat_completed_call(self):
        s=self.agent({'city':'Москва','budget_max':20_000_000,'payment':'cash'})
        x=listing(id='verified-target',city='Москва')
        s.search={'listings':[x],'pool':[x]};s.shown=[x];s.call_targets=[x['id']]
        s.red_lines_ok=True;s.request_intent='call'
        self.assertEqual(workflow.next_search_action(s),'call_seller')
        self.assertEqual(workflow.planned_call(s,'call_seller').arguments,{'listing_id':x['id']})
        s.calls[x['id']]={'price':x['price']}
        self.assertIsNone(workflow.next_search_action(s))
        s.calls.clear();s.operations={'attempt':{'name':'call_seller','arguments':{'listing_id':x['id']},
            'request_seq':s.request_review_seq,'status':'completed','result':{'called':False}}}
        self.assertIsNone(workflow.next_search_action(s))

    def test_state_grounding_distinguishes_uncertainty_from_unrestricted_per_clause(self):
        from backend.agent.clarification import ground_state
        for key in ('budget_max','rooms','districts','housing'):
            self.assertEqual(ground_state(key,'unrestricted','Пока не знаю, ещё не решил'),'unknown')
            self.assertEqual(ground_state(key,'unknown','Неважно, убери ограничение'),'unrestricted')
        text='Бюджет неважен, комнатность пока не знаю.'
        self.assertEqual(ground_state('budget_max','unknown',text),'unrestricted')
        self.assertEqual(ground_state('rooms','unrestricted',text),'unknown')

    def test_spelled_numbers_do_not_conflict_with_digits_in_other_conditions(self):
        from backend.agent import input_frame
        from backend.agent.request_schema import CHANGE_SCHEMA
        text='Выше двенадцатого этажа, бюджет до 20 млн'
        frame=input_frame.complete_frame({'intent':'refine','fields':{'filters':{'value':[
            {'field':'floor','op':'gt','values':['12'],'priority':'required'}],'source':text}}},CHANGE_SCHEMA)
        payload={'messages':[{'text':text}],'supported_cities':sources.SUPPORTED_CITIES,'ambiguous_catalog_names':{}}
        r=input_frame.decode(frame,merge_profile({}),payload,CHANGE_SCHEMA)
        self.assertEqual(r['changes']['filters'][0]['values'],['12'])
        frame['fields']['filters']['value'][0]={'field':'delivery_year','op':'eq','values':['2026'],'priority':'required'}
        with self.assertRaises(ValueError):input_frame.decode(frame,merge_profile({}),payload,CHANGE_SCHEMA)
        from backend.agent.numbers import word_numbers
        self.assertEqual(word_numbers('стоимость, вторичка, однако, триумф'),set())

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
        s.llm=Model();s.active_operation={'name':'ask_client','status':'waiting'}
        s.pending_decisions={'clarify':{'kind':'clarify','text':'Какой этаж для вас предпочтительнее?','payload':{'chips':['Любой']},'status':'waiting'}}
        e=s.push('message',role='user',text='Любой');s.accept_request('Любой',e['seq'])
        r=await understanding.prepare(s)
        self.assertEqual(r['decision'],'apply');apply_request(s,r['changes'])
        self.assertFalse(s.profile['not_first_floor']);self.assertFalse(s.profile['not_last_floor'])
        self.assertEqual(s.profile['budget_max'],20_000_000)

    async def test_action_or_continue_route_cannot_skip_new_conditions(self):
        class Model(ScriptedLLM):
            async def chat(model,messages,**kw):
                if kw.get('tag')=='request_review':
                    if model.route=='continue':return LLMResponse('',[ToolCall('continue_input',{})])
                    return LLMResponse('',[ToolCall('read_selection',{'operation':'rank','scope':'all','limit':None,
                        'aggregates':[],'group_by':None,'order_by':'value_score','direction':'desc','keep_order':False,
                        'update_conditions':False,'count_source':None})])
                if kw.get('tag')=='profile_extract':
                    return LLMResponse(json.dumps({'fields':[
                        {'key':'housing','value':'both','source':text},{'key':'payment','value':'cash','source':text}],
                        'criteria':[],'states':[],'filters':[],'places':[],'unsupported':[],'requested_card':[],
                        **({'selection':[]} if model.route=='continue' else {})}))
                return await super().chat(messages,**kw)
        text='Новостройка или вторичка, за наличные. Остальные условия сохрани.'
        for route in ('rank','continue'):
            s=self.agent({'city':'Москва','rooms':3});s.shown=[listing()];s.search={'pool':list(s.shown)};s.llm=Model();s.llm.route=route
            e=s.push('message',role='user',text=text);s.accept_request(text,e['seq'])
            r=await understanding.prepare(s)
            self.assertEqual(r['changes']['housing'],'both');self.assertEqual(r['changes']['payment'],'cash')
            self.assertNotIn('rooms_options',r['changes'])

    def test_research_query_reuses_numeric_executor_and_rejects_invalid_metrics(self):
        rows=[listing(id='a',area=40),listing(id='b',area=80)]
        result=research.execute(rows,{'respect_budget':True,'group_by':None,'aggregates':[{'field':'area','function':'max'}]})
        self.assertEqual(result['aggregates'][0]['value'],80)
        with self.assertRaises(ValueError):research.query({'respect_budget':True,'group_by':None,'aggregates':[{'field':'address','function':'max'}]})

    def test_native_predicates_and_states_need_no_embedded_json(self):
        from backend.agent import input_frame,profile_patch
        from backend.agent.request_schema import CHANGE_SCHEMA
        text='Москва, выше 10 этажа у парка, бюджет неважен'
        inputs=[{'text':text}]
        contract=profile_patch.schema(input_frame.schema(CHANGE_SCHEMA,inputs))
        data={'fields':[],'criteria':[],'places':[],'unsupported':[],'selection':[],'requested_card':[],
              'states':[{'key':'budget_max','state':'unrestricted','source':text}],
              'filters':[{'field':'floor','op':'gt','values':['10'],'priority':'required','source':text},
                         {'field':'nearby:park','op':'exists','values':[],'priority':'required','source':text}]}
        frame=profile_patch.decode(data,contract,inputs)
        self.assertEqual(frame['fields']['field_states']['value'],{'budget_max':'unrestricted'})
        self.assertEqual(frame['fields']['filters']['value'][0]['op'],'gt')
        self.assertEqual(frame['criteria']['poi:park'],'required')
        markets=deepcopy(data)
        markets['filters']=[{'field':'is_new','op':'in','values':['true','false'],'priority':'required','source':text}]
        parsed=profile_patch.decode(markets,contract,inputs)
        self.assertEqual(parsed['fields']['housing']['value'],'both')
        self.assertNotIn('filters',parsed['fields'])
        generation=profile_patch.generation_schema(contract,inputs)
        self.assertNotIn('filters',generation['properties']['fields']['items']['properties']['key']['enum'])
        uncertain=deepcopy(data)
        for group in ('states','filters'):uncertain[group]=[]
        uncertain['fields']=[{'key':'budget_max','action':'remove','value':'','source':'Бюджет пока не определил'}]
        decoded=profile_patch.decode(uncertain,contract,[{'text':'Бюджет пока не определил'}])
        self.assertEqual(decoded['fields']['field_states']['value']['budget_max'],'unknown')
        data['states'][0]['source']='выдуманная цитата'
        with self.assertRaises(ValueError):profile_patch.decode(data,contract,inputs)

    async def test_bound_any_choice_clears_only_addressed_property(self):
        for field in ('floor','area','budget_max','rooms'):
            with self.subTest(field=field):
                s=self.agent({'city':'Москва','rooms':2,'budget_max':20_000_000,'area_min':60,'not_first_floor':True})
                s.active_operation={'name':'ask_client','arguments':{'field':field},'status':'waiting'}
                s.pending_decisions={'clarify':{'kind':'clarify','text':'Выберите условие','payload':{'chips':['Любой']},'status':'waiting'}}
                e=s.push('message',role='user',text='Любой');s.accept_request('Любой',e['seq'])
                r=await understanding.prepare(s);apply_request(s,r['changes'])
                self.assertEqual(s.profile['city'],'Москва')
                self.assertEqual(s.profile['budget_max'],None if field=='budget_max' else 20_000_000)
                self.assertEqual(s.profile['rooms'],None if field=='rooms' else 2)
                self.assertEqual(s.profile['area_min'],None if field=='area' else 60)
                self.assertEqual(s.profile['not_first_floor'],field!='floor')
