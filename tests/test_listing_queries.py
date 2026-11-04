"""Поведение общего языка запросов и регрессии свободного изменения подбора."""
import asyncio
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import input_frame, listing_query as Q, session, workflow
from backend.agent.actions import request, search, deal
from backend.agent.criteria import evaluate, selectable
from backend.agent.profile import merge_profile, passes_hard_filters
from backend.agent.request_schema import CHANGE_SCHEMA
from tests.input_contract import slots
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


def condition(field, op, *values, priority='required'):
    return {'field':field,'op':op,'values':[str(v) for v in values],'priority':priority}


class QueryEngineTest(unittest.TestCase):
    def test_map_existence_is_checked_after_enrichment(self):
        p=merge_profile({'city':'Москва','rooms':2,'budget_max':30_000_000,
                         'filters':[condition('nearby:kindergarten','exists')]})
        x=listing(city='Москва',rooms=2,price=10_000_000)
        self.assertTrue(passes_hard_filters(x,p))
        self.assertEqual(evaluate(p,x)['status'],'rejected')
        x['nearby']={'kindergarten':{'name':'Садик','dist':350}}
        self.assertEqual(evaluate(p,x)['status'],'confirmed')

    def test_composed_year_range_order_limit_and_aggregate_use_real_data(self):
        rows=[listing(id=f'cian-{i}',deadline=f'4 кв. {2026+i}',year=None,area=50+i*10) for i in range(4)]
        query={**Q.default_query(),'limit':1,'filters':[condition('delivery_year','between',2027,2028)],
               'sort':[{'field':'area','direction':'desc'}],'aggregates':[{'field':'area','function':'mean'}]}
        before=deepcopy(rows)
        result=Q.execute(rows,query)
        self.assertEqual([x['id'] for x in result['items']],['cian-2'])
        self.assertEqual((result['total'],result['remaining']), (2,1))
        self.assertEqual(result['aggregates'][0]['value'],65)
        self.assertEqual(rows,before)

    def test_unknowns_sort_last_in_both_directions(self):
        rows=[listing(id='unknown',kitchen=None),listing(id='small',kitchen=5),listing(id='big',kitchen=15)]
        for direction,expected in [('asc',['small','big','unknown']),('desc',['big','small','unknown'])]:
            q={**Q.default_query(),'sort':[{'field':'kitchen','direction':direction}]}
            self.assertEqual([x['id'] for x in Q.execute(rows,q)['items']],expected)

    def test_year_of_delivery_is_not_year_of_construction(self):
        x=listing(deadline='4 кв. 2027',year=None)
        p=merge_profile({'filters':[condition('delivery_year','eq',2027)]})
        result=evaluate(p,x)
        self.assertEqual(result['status'],'confirmed')
        self.assertTrue(result['checks'][0]['status'])
        self.assertIn('сдачи',result['checks'][0]['label'])
        self.assertIsNone(Q.field_value(x,'year'))
        p['filters']=[condition('delivery_year','eq',2028)]
        self.assertEqual(evaluate(p,x)['status'],'rejected')

    def test_unknown_required_delivery_cannot_be_presented_as_matching(self):
        x=listing(deadline=None,year=2027)
        p=merge_profile({'filters':[condition('delivery_year','eq',2027)]})
        s=session.Session('test','',ScriptedLLM());s.prefs.sync(p)
        self.assertEqual(evaluate(p,x)['status'],'unverified')
        self.assertFalse(selectable(x,s.prefs))

    def test_disjoint_years_and_derived_price_per_metre(self):
        rows=[listing(id=str(y),delivery_year=y,price=10_000_000,area=50) for y in (2026,2027,2028,2029)]
        q={**Q.default_query(),'filters':[condition('delivery_year','in',2027,2029),condition('price_per_m2','lte',200000)]}
        self.assertEqual([x['id'] for x in Q.execute(rows,q)['items']],['2027','2029'])

    def test_liked_shown_and_more_are_independent_scopes(self):
        rows=[listing(id=str(i)) for i in range(6)]
        self.assertEqual([x['id'] for x in Q.execute(rows,{**Q.default_query(),'scope':'liked'},liked=['0','2'])['items']],['0','2'])
        self.assertEqual([x['id'] for x in Q.execute(rows,{**Q.default_query(),'scope':'shown'},shown=['3'])['items']],['3'])
        self.assertEqual([x['id'] for x in Q.execute(rows,{**Q.default_query(),'mode':'more'},seen=['0','1','2'])['items']],['3','4','5'])

    def test_untrusted_fields_operations_and_invalid_values_are_rejected(self):
        for f in (condition('price; DROP TABLE users','eq',1),condition('area','eval','anything'),
                  condition('area','gte','nan'),condition('delivery_year','between',2028,2027),
                  condition('price','eq'),condition('is_new','eq','perhaps')):
            with self.subTest(filter=f),self.assertRaises((ValueError,KeyError)):
                Q.validate_query({**Q.default_query(),'filters':[f]})

    def test_grouping_reports_counts_and_aggregates_for_each_district(self):
        rows=[listing(district='А',area=40),listing(district='А',area=60),listing(district='Б',area=90)]
        result=Q.execute(rows,{**Q.default_query(),'group_by':'district','aggregates':[{'field':'area','function':'mean'}]})
        self.assertEqual([(g['value'],g['count'],g['aggregates'][0]['value']) for g in result['groups']], [('А',2,50),('Б',1,90)])

    def test_invalid_source_number_is_unknown_and_preference_does_not_exclude(self):
        rows=[listing(id='small',area=35,kitchen='не указано'),listing(id='large',area=70,kitchen=12)]
        q={**Q.default_query(),'filters':[condition('area','gte',60,priority='preferred')],
           'sort':[{'field':'kitchen','direction':'asc'}]}
        result=Q.execute(rows,q)
        self.assertEqual([x['id'] for x in result['items']],['large','small'])
        self.assertIsNone(Q.field_value(rows[0],'kitchen'))

    def test_filter_updates_replace_only_mentioned_fields_and_support_removal(self):
        p=merge_profile({'filters':[condition('delivery_year','eq',2027),condition('area','gte',40)]})
        changed=request.updated_profile(p,{'filters':[condition('delivery_year','eq',2028)]})
        self.assertEqual({f['field'] for f in changed['filters']},{'delivery_year','area'})
        cleared=request.updated_profile(changed,{'filters':[condition('delivery_year','eq',priority='remove')]})
        self.assertEqual([f['field'] for f in cleared['filters']],['area'])

    def test_required_centre_rejects_distant_moscow_but_preference_does_not(self):
        far=listing(city='Москва',lat=55.44,lng=37.29)
        p=merge_profile({'city':'Москва','rooms':2,'budget_max':30_000_000,'prefer_center':True})
        self.assertTrue(passes_hard_filters(far,p))
        p['center_required']=True
        self.assertFalse(passes_hard_filters(far,p))
        self.assertTrue(passes_hard_filters(listing(city='Москва',lat=55.7539,lng=37.6208),p))

    def test_unmentioned_empty_fields_do_not_erase_existing_constraints(self):
        profile=merge_profile({'city':'Москва','center_required':True,'districts':['Арбат'],'rooms_options':[1,2]})
        frame=slots(fields={'center_required':False,'districts':[],'rooms_options':[]},
                    intent='selection',selection={**Q.default_query(),'limit':3})
        result=input_frame.decode(frame,profile,{'messages':[{'text':'Три лучших','options':[]}],
            'supported_cities':['Москва'],'ambiguous_catalog_names':{}},CHANGE_SCHEMA)
        self.assertEqual(result['changes'],{})
        self.assertEqual(result['selection']['limit'],3)

    def test_invalid_place_is_repaired_instead_of_asking_about_school_city(self):
        frame=slots(places=['школа'],criteria={'poi:school':'required'})
        with self.assertRaises(ValueError):
            input_frame.decode(frame,{'city':'Казань'},{'messages':[{'text':'школа рядом','options':[]}],
                'supported_cities':['Казань'],'ambiguous_catalog_names':{}},CHANGE_SCHEMA)

    def test_hold_can_save_seller_questions_without_permitting_any_action(self):
        text='Уточни шумоизоляцию, пока не звони'
        frame=slots(fields={'seller_questions':['Как со звукоизоляцией?']},intent='hold')
        frame['fields']['seller_questions']['source']='Уточни шумоизоляцию'
        result=input_frame.decode(frame,{'city':'Казань'},{'messages':[{'text':text,'options':[]}],
            'supported_cities':['Казань'],'ambiguous_catalog_names':{}},CHANGE_SCHEMA)
        self.assertEqual(result['intent'],'hold')
        self.assertEqual(result['changes']['seller_questions'],['Как со звукоизоляцией?'])
        self.assertIsNone(result['call_ids'])

    def test_selection_command_cannot_change_room_count(self):
        text='4 а не 3'
        frame=slots(fields={'rooms':4,'rooms_options':[4]},intent='selection',
                    selection={**Q.default_query(),'limit':4})
        for slot in frame['fields'].values():
            if slot is not None: slot['source']=text
        with self.assertRaisesRegex(ValueError,'selection меняет выдачу'):
            input_frame.decode(frame,{'rooms':2,'rooms_options':[2]},
                {'messages':[{'text':text,'options':[]}]},CHANGE_SCHEMA)

    def test_selection_command_cannot_change_family_or_profile_criteria(self):
        for frame in [slots(intent='selection',fields={'children':4}),
                      slots(intent='selection',criteria={'not_first_floor':'required'})]:
            for slot in frame['fields'].values():
                if slot is not None:slot['source']='4 а не 3'
            with self.assertRaisesRegex(ValueError,'selection меняет'):
                input_frame.decode(frame,{'rooms':2},{'messages':[{'text':'4 а не 3','options':[]}],
                    'supported_cities':['Москва'],'ambiguous_catalog_names':{}},CHANGE_SCHEMA)


class DialogueStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')

    def agent(self):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':30_000_000,'payment':'cash'})
        s.red_lines_ok=True;s.prefs.sync(s.profile)
        rows=[listing(id=f'cian-{i}',price=7_000_000+i*1000) for i in range(25)]
        s.search={'listings':rows,'pool':rows,'comps':rows,'meta':{},'pages':3,'shown_ids':set(),
                  'named':[],'strict':False,'report':[]}
        return s

    async def test_more_with_ten_likes_shows_new_objects_and_honest_remaining(self):
        s=self.agent()
        s.prefs.liked=[x['id'] for x in s.search['pool'][:10]]
        s.search['shown_ids']=set(s.prefs.liked)
        s.shown=s.search['pool'][:10]
        s.selection_query={**Q.default_query(),'mode':'more'}
        s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text','liked':s.prefs.liked}})
        await search.show_selection(s)
        self.assertEqual(len(s.shown),10)
        self.assertFalse(set(x['id'] for x in s.shown)&set(s.prefs.liked))
        event=next(e for e in s.history if e.get('kind')=='selection')
        self.assertEqual(event['data']['remaining'],5)
        self.assertEqual(len(s.prefs.liked),10)

    async def test_top_three_uses_current_pool_without_new_search_or_call_permission(self):
        s=self.agent();s.shown=s.search['pool'][:10]
        s.selection_query={**Q.default_query(),'scope':'shown','limit':3,'sort':[{'field':'area','direction':'desc'}]}
        s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text'}})
        await search.show_selection(s)
        self.assertEqual(len(s.shown),3)
        self.assertFalse(workflow.available(s,'call_seller'))
        messages=[e['text'] for e in s.history if e['type']=='message' and e.get('role')=='agent']
        self.assertTrue(any('Отобрал для вас 3' in text for text in messages))
        self.assertTrue(any('по убыванию' in text for text in messages))

    async def test_four_largest_can_expand_previous_three_without_changing_rooms(self):
        s=self.agent();s.shown=s.search['pool'][:3]
        s.selection_query={**Q.default_query(),'limit':4,'sort':[{'field':'area','direction':'desc'}]}
        s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text'}})
        await search.show_selection(s)
        self.assertEqual(len(s.shown),4)
        self.assertEqual(s.profile['rooms'],2)

    async def test_narrow_scope_reports_shortfall_honestly(self):
        s=self.agent();s.shown=s.search['pool'][:3]
        s.selection_query={**Q.default_query(),'scope':'shown','limit':4}
        s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text'}})
        await search.show_selection(s)
        self.assertEqual(len(s.shown),3)
        self.assertTrue(any('Запрошено 4' in e.get('text','') for e in s.history))

    async def test_refinement_revokes_old_call_permission_and_preserves_budget(self):
        s=self.agent();s.shown=s.search['pool'][:10];s.call_targets=['cian-1']
        request.apply_request(s,{'filters':[condition('delivery_year','eq',2027)]})
        self.assertTrue(s.red_lines_ok)
        self.assertFalse(s.shown)
        self.assertFalse(s.call_targets)
        self.assertFalse(workflow.available(s,'call_seller'))

    async def test_direct_request_to_call_is_not_replaced_by_a_new_selection(self):
        s=self.agent();s.shown=s.search['pool'][:3]
        s.request_intent='call';s.call_targets=[s.shown[1]['id']]
        self.assertTrue(workflow.available(s,'call_seller'))
        self.assertFalse(workflow.available(s,'show_selection'))
        self.assertFalse(workflow.available(s,'search_listings'))

    async def test_hold_with_explicit_budget_change_saves_it_but_requires_new_consent(self):
        s=self.agent();s.call_targets=['cian-1']
        request.apply_request(s,{'budget_max':12_000_000})
        s.request_intent='hold'
        self.assertFalse(workflow.available(s,'ask_client'))
        self.assertEqual(s.profile['budget_max'],12_000_000)
        self.assertFalse(s.red_lines_ok)
        self.assertFalse(workflow.available(s,'search_listings'))
        self.assertFalse(workflow.available(s,'call_seller'))

    async def test_question_switches_typing_off_and_old_decision_id_cannot_resolve_new(self):
        s=self.agent();q=asyncio.Queue();s.subscribers.add(q)
        s.signal('typing',on=True)
        task=asyncio.create_task(s.ask('clarify','Уточню','Какой район?'))
        await asyncio.sleep(0)
        events=[]
        while not q.empty():events.append(q.get_nowait())
        self.assertEqual(s.activity,'awaiting_user')
        self.assertTrue(any(e['type']=='typing' and e['on'] is False for e in events))
        decision=next(e for e in events if e['type']=='confirm')['decision_id']
        self.assertFalse(s.resolve('clarify',True,{},'obsolete'))
        self.assertFalse(task.done())
        self.assertTrue(s.resolve('clarify',True,{},decision))
        await task

    async def test_program_of_call_combines_missing_facts_and_user_questions_without_dialling(self):
        s=self.agent();s.shown=s.search['pool'][:1];s.call_targets=[s.shown[0]['id']]
        s.profile['seller_questions']=['Как со звукоизоляцией?','Были ли перепланировки?']
        s.ask=AsyncMock(return_value={'approved':False,'data':{}})
        with patch.object(sources,'verify',AsyncMock(return_value={'alive':True,'listing':None,'note':'test'})):
            result=await deal.call_seller(s,s.shown[0]['id'])
        self.assertFalse(result['called'])
        questions=s.ask.call_args.kwargs['questions']
        self.assertTrue(any(q['question']=='Как со звукоизоляцией?' for q in questions))
        self.assertTrue(any(q['question']=='Были ли перепланировки?' for q in questions))
