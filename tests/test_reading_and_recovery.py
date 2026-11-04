import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock,patch

from backend import db,sources
from backend.agent import session,selection_request as R,listing_query as Q,understanding as U,input_frame,qa
from backend.agent.actions import search
from backend.agent.profile import merge_profile
from backend.agent.request_schema import CHANGE_SCHEMA
from tests.fakes import ScriptedLLM
from tests.input_contract import slots
from tests.test_llm_and_agent import listing
from tests.test_understanding import ReviewModel


def request(operation='rank',scope='all',limit=2,field='area',direction='desc'):
    return {'operation':operation,'scope':scope,'limit':limit,'sort':[{'field':field,'direction':direction}],
            'aggregates':None,'group_by':None}


class ReadingAndRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms_options':[1,2],'budget_max':9_000_000,'payment':'cash'})
        s.prefs.sync(s.profile);s.red_lines_ok=True
        rows=[listing(id='cian-1',city='Казань',rooms=1,price=4_000_000,area=40),
              listing(id='cian-2',city='Казань',rooms=2,price=6_000_000,area=80)]
        s.search={'listings':rows,'pool':rows,'comps':rows,'meta':{},'pages':3,'shown_ids':set(),
                  'named':[],'strict':False,'report':[]}
        s.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text'}})
        return s

    async def show(self,s):
        s.selection_query={**Q.default_query(),'limit':2}
        await search.show_selection(s)

    async def test_compare_uses_price_per_meter_without_replacing_two_cards(self):
        s=self.agent();await self.show(s)
        before=deepcopy(s.shown);committed=deepcopy(s.profile['_selection_query'])
        s.selection_query,s.selection_operation=R.compile_request(request('compare','shown',1,'price_per_m2','asc'))
        with patch.object(sources,'collect',AsyncMock()) as collect:
            await search.show_selection(s,query={**Q.default_query(),'filters':[{'field':'price_per_m2','op':'lte','values':['2'],'priority':'required'}]})
        collect.assert_not_awaited()
        self.assertEqual(s.shown,before)
        self.assertEqual(s.profile['_selection_query'],committed)
        self.assertEqual(len([e for e in s.history if e.get('kind')=='selection' and e['type']=='result']),1)
        text=' '.join(e.get('text','') for e in s.history if e.get('role')=='agent')
        self.assertIn('75 000 ₽/м²',text)
        self.assertIn('100 000 ₽/м²',text)
        self.assertIn('Карточки оставил на месте',text)

    async def test_empty_candidate_does_not_commit_or_erase_and_next_query_recovers(self):
        s=self.agent();await self.show(s)
        cards=deepcopy(s.shown);saved=deepcopy(s.profile['_selection_query'])
        s.selection_query={**Q.default_query(),'filters':[{'field':'price_per_m2','op':'lte','values':['2'],'priority':'required'}]}
        await search.show_selection(s)
        self.assertEqual(s.shown,cards)
        self.assertEqual(s.selection_query,saved)
        self.assertEqual(s.profile['_selection_query'],saved)
        self.assertTrue(any(e['type']=='selection_notice' for e in s.history))
        self.assertFalse(any(e['type']=='result' and e.get('kind')=='selection' and not e['data']['cards'] for e in s.history))
        s.selection_query,s.selection_operation=R.compile_request(request())
        await search.show_selection(s)
        self.assertEqual(len(s.shown),2)
        self.assertIsNone(s.last_issue)

    async def test_resume_skips_legacy_empty_result_and_recovers_exact_snapshot(self):
        s=self.agent();await self.show(s)
        s.push('result',step=4,kind='selection',data={'cards':[],'query':{**Q.default_query(),'scope':'shown'}})
        s.profile['_selection_query']={**Q.default_query(),'scope':'shown'}
        s.remember()
        restored=session.restore_session(s.id,'Какая из этих двух дешевле за метр?',ScriptedLLM())
        self.assertEqual([x['id'] for x in restored.shown],[x['id'] for x in s.shown])
        self.assertEqual(restored.selection_query['scope'],'all')
        restored.selection_query,restored.selection_operation=R.compile_request(request('compare','shown',1,'price_per_m2','asc'))
        restored.ask=AsyncMock(return_value={'approved':True,'data':{'action':'text'}})
        await search.show_selection(restored)
        self.assertEqual(len(restored.shown),2)

    async def test_failed_input_is_not_reparsed_with_a_question_about_failure(self):
        s=self.agent();e=s.push('message',role='user',text='старое неразобранное')
        s.accept_request(e['text'],e['seq'])
        U.failed_input(s,U.UnderstandingError('invalid model object'))
        s.llm=ReviewModel(slots(intent='question'))
        with patch.object(qa,'answer',AsyncMock()) as answer:
            self.assertEqual(await s.receive_text('А что надо уточнить?'),'question')
            await asyncio.sleep(0)
            answer.assert_awaited_once()
        payload=json.loads(s.llm.seen[0][-1]['content'])['data']
        self.assertEqual([r['text'] for r in payload['messages']],['А что надо уточнить?'])
        self.assertIn('на моей стороне',s.last_issue['message'])

    async def test_partial_city_is_a_specific_question_not_an_internal_error(self):
        text='1-2 комнаты ближе к центральной улице нижнего'
        frame=slots(places=['Нижний'],fields={'rooms_options':[1,2]})
        frame['places'][0]['words'][0]['source']='нижнего'
        frame['fields']['rooms_options']['source']='1-2'
        result=input_frame.decode(frame,{}, {'messages':[{'text':text,'options':[]}],
            'supported_cities':['Нижний Новгород','Нижний Тагил'],'ambiguous_catalog_names':{}},CHANGE_SCHEMA)
        self.assertEqual(result['decision'],'clarify')
        self.assertIn('Нижний Новгород',result['question'])
        self.assertIn('Нижний Тагил',result['question'])

    async def test_actual_batches_drive_progress_counts(self):
        s=self.agent()
        rows=[listing(id=f'cian-{i}',city='Казань',rooms=1,price=4_000_000) for i in range(5)]
        async def collect(profile,on_batch=None):
            on_batch(rows[:2]);await asyncio.sleep(0)
            on_batch(rows[2:]);return rows,[]
        with patch.object(sources,'collect',collect),patch.object(sources,'stored',return_value=[]),patch.object(sources,'comparables',return_value=[]),patch.object(sources,'more',AsyncMock(return_value=[])):
            await search.search_listings(s)
        events=[e for e in s.history if e['type']=='search_progress' and e.get('phase')=='collect']
        self.assertEqual([e['found'] for e in events],[2,5])
        self.assertEqual([e['candidates'] for e in events],[2,5])

    def test_model_cannot_supply_a_predicate_in_a_comparison_request(self):
        with self.assertRaises(ValueError):
            R.compile_request({**request('compare','shown',1,'price_per_m2','asc'),
                'filters':[{'field':'price_per_m2','op':'lte','values':['2'],'priority':'required'}]})

    def test_number_of_candidates_does_not_create_price_or_area_thresholds(self):
        for operation in ('rank','compare','analyze','more'):
            query,_=R.compile_request(request(operation))
            self.assertEqual(query['filters'],[])

    def test_studio_has_a_separate_explicit_slot(self):
        f=slots(fields={'rooms_options':[1,2]})
        self.assertNotIn('rooms',f['fields'])
        self.assertIsNone(f['fields']['studio_requested'])
        self.assertEqual(input_frame.basic_fields(CHANGE_SCHEMA)['rooms_options']['items']['minimum'],1)
