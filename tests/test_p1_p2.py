import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

from fastapi.testclient import TestClient
from sqlalchemy import text
from backend import db,events,main,phone,phone_access,readiness,telemetry,sources
from backend.db import repo,migrations
from backend.agent import actions,capabilities,conditions,coverage,input_frame,session,understanding
from backend.agent.actions.request import apply_request
from backend.agent.profile import merge_profile
from backend.agent.request_schema import CHANGE_SCHEMA
from backend.config import settings
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class P1P2(unittest.IsolatedAsyncioTestCase):
    def setUp(self):db.configure('sqlite://')

    def agent(self):
        s=session.create_session('',ScriptedLLM())
        s.profile=merge_profile({'city':'Казань','rooms_options':[2],'budget_max':9_000_000,'payment':'cash'})
        s.profile['_approved_budget_max']=9_000_000;s.red_lines_ok=True;s.prefs.sync(s.profile)
        s.remember()
        return s

    def test_one_catalog_controls_schemas_permissions_and_unknown_tools(self):
        s=self.agent();actions._load()
        self.assertEqual(set(actions.REGISTRY),set(capabilities.POLICIES))
        rows=capabilities.catalog(s)
        self.assertEqual(len(rows),18)
        for row in rows:
            self.assertEqual(row['allowed'],capabilities.decision(s,row['name'])[0])
            if not row['allowed']:self.assertTrue(row['reason'])
        self.assertEqual(actions.REGISTRY['call_seller'].parameters['required'],['listing_id'])
        entry=actions.REGISTRY['market_prices']
        with patch.dict(actions.REGISTRY,new_tool=replace(entry,name='new_tool')):
            self.assertFalse(capabilities.decision(s,'new_tool')[0])
            self.assertNotIn('new_tool',[t.name for t in actions.tools()])

    async def test_bounded_delivery_recovers_critical_events_from_storage(self):
        s=self.agent();queue=events.EventBuffer(3);s.subscribers.add(queue)
        seq=s.seq
        for i in range(5):s.push('message',role='agent',text=str(i))
        self.assertEqual(queue.qsize(),1)
        self.assertEqual(queue.get_nowait()['type'],'resync_required')
        self.assertNotIn(queue,s.subscribers)
        self.assertEqual([e['text'] for e in db.required(repo.chat_events,s.id,seq)],list(map(str,range(5))))

    def test_progress_is_coalesced_without_moving_a_critical_fact(self):
        q=events.EventBuffer(3)
        q.put_nowait({'type':'search_progress','seq':1,'found':2})
        q.put_nowait({'type':'message','seq':2,'text':'факт'})
        q.put_nowait({'type':'search_progress','seq':3,'found':5})
        self.assertEqual(q.qsize(),2)
        self.assertEqual(q.get_nowait()['seq'],2)
        self.assertEqual(q.get_nowait()['found'],5)

    def test_admission_limit_does_not_accept_a_command_that_would_be_lost(self):
        s=self.agent()
        db.required(repo.accept_command,s.id,'one','message','a',{'text':'one'},capacity=1)
        with self.assertRaises(db.CommandCapacityError):
            db.required(repo.accept_command,s.id,'two','message','b',{'text':'two'},capacity=1)
        self.assertEqual([r['id'] for r in db.required(repo.queued_commands,s.id)],['one'])
        again=db.required(repo.accept_command,s.id,'one','message','a',{'text':'one'},capacity=1)
        self.assertEqual(again['id'],'one')

    async def test_second_page_failure_is_not_market_exhaustion(self):
        s=self.agent();x=listing()
        s.search={'listings':[x],'pool':[x],'comps':[x],'meta':{},'pages':1,'named':[],'strict':False,
                  'report':[{'source':'cian','status':'live','count':1}]}
        batch=sources.PageBatch([],reports=[{'source':'cian','status':'unavailable','page':2,'count':0}])
        from backend.agent.actions.search import _more_pages
        with patch.object(sources,'more',AsyncMock(return_value=batch)):
            await _more_pages(s,lambda:False)
        r=coverage.report(s)
        self.assertFalse(r['whole_market'])
        self.assertIn('source_unavailable',r['reasons'])
        self.assertIn('page_unavailable',r['reasons'])

    async def test_global_search_budget_is_reported_without_discarding_a_real_snapshot(self):
        profile={'city':'Казань','rooms':2,'budget_max':9_000_000,'housing':'secondary','_source_deadline':time.monotonic()-1}
        x=listing()
        with patch.object(sources.cian,'search',AsyncMock()) as fetch,\
             patch.object(sources.cian,'load_snapshot',return_value=([x],'2026-11-04T00:00:00+00:00')):
            rows,reports=await sources.collect(profile)
        fetch.assert_not_awaited();self.assertEqual(rows[0]['id'],x['id'])
        self.assertEqual(reports[0]['status'],'snapshot')
        self.assertEqual(reports[0]['reason'],'search_budget_exhausted')
        self.assertEqual(rows[0]['provenance']['observed_at'],'2026-11-04T00:00:00+00:00')

    def test_match_proof_keeps_source_and_time_and_unknown_is_separate(self):
        s=self.agent();apply_request(s,{'area_min':50,'year_min':2020})
        x=listing(year=None,provenance={'source':'cian','observed_at':'2026-11-05T00:00:00+00:00'})
        s.search={'listings':[x],'report':[]};r=coverage.report(s)
        self.assertEqual(r['checks'],{'confirmed':1,'unknown':1})
        self.assertEqual(r['proofs']['with_time'],1)
        self.assertIn('unchecked_facts',r['reasons'])

    def test_generated_set_remove_sequences_keep_canonical_state_and_pool(self):
        for seed in range(15):
            s=self.agent();rows=[listing(id=f'x{i}',area=35+i*5) for i in range(12)]
            s.search={'listings':rows,'pool':list(rows),'shown_ids':set(),'meta':{}}
            rng=random.Random(seed);threshold=None
            for _ in range(25):
                threshold=rng.choice([None,40,60,85,1000])
                if threshold is None:change={'clear_criteria':['area_min']}
                elif rng.choice([True,False]):change={'area_min':threshold}
                else:change={'filters':[{'field':'area','op':'gte','values':[str(threshold)],'priority':'required'}]}
                apply_request(s,change)
                compiled=[c for c in conditions.compile_profile(s.profile) if c['field']=='area']
                self.assertEqual(len(compiled),int(threshold is not None))
                self.assertEqual({x['id'] for x in s.search['pool']},{x['id'] for x in rows if threshold is None or x['area']>=threshold})
                self.assertTrue(s.red_lines_ok);self.assertEqual(s.profile['city'],'Казань')

    def test_recorded_bad_model_responses_fail_without_profile_mutation(self):
        from backend.agent import profile_patch
        incidents=json.loads((Path(__file__).parent/'data/model_incidents.json').read_text())
        for item in incidents:
            with self.subTest(id=item['id']):
                s=self.agent();before=deepcopy(s.profile);inputs=[{'seq':1,'text':item['input']}]
                with self.assertRaises(ValueError):
                    if item['stage']=='profile_extract':
                        profile_patch.decode(item['frame'],profile_patch.schema(input_frame.schema(CHANGE_SCHEMA,inputs)),inputs)
                    else:
                        tools=understanding.semantic_tools(input_frame.sparse_schema(CHANGE_SCHEMA,inputs),inputs,[])
                        understanding.frame_from_call(ToolCall(**item['call']),tools,inputs)
                self.assertEqual(s.profile,before)

    def test_unknown_database_version_and_changed_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/app.db';db.configure('sqlite:///'+path)
            with sqlite3.connect(path) as conn:conn.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
            with self.assertRaises(migrations.SchemaError):db.configure('sqlite:///'+path)
            with sqlite3.connect(path) as conn:
                conn.execute('DELETE FROM schema_migrations')
                conn.execute("INSERT INTO schema_migrations VALUES (99,'new','now')")
            with self.assertRaises(migrations.SchemaError):db.configure('sqlite:///'+path)
        db.configure('sqlite://')

    def test_readiness_distinguishes_storage_from_a_live_process(self):
        with patch.object(telemetry,'dependencies',return_value={}):
            self.assertTrue(readiness.report(ScriptedLLM())['ready'])
            with patch.object(db,'session',side_effect=RuntimeError('unavailable')):
                r=readiness.report(ScriptedLLM())
                self.assertFalse(r['ready']);self.assertFalse(r['database']['ready'])
        self.assertFalse(readiness.report(None)['ready'])

    def test_idle_session_cache_is_bounded_but_saved_dialogues_remain_readable(self):
        with patch.object(session,'SESSIONS',{}),patch.object(session,'settings',replace(settings,max_cached_dialogues=3)):
            originals=[self.agent() for _ in range(6)]
            self.assertEqual(len(session.SESSIONS),3)
            old=originals[0]
            restored=session.restore_session(old.id,'',old.llm)
            self.assertEqual(restored.profile,old.profile)
            self.assertEqual(len(session.SESSIONS),3)
            restored.idle_since=0
            restored.subscribers.add(events.EventBuffer(8))
            self.agent()
            self.assertIs(session.SESSIONS[restored.id],restored)
            for agent in session.SESSIONS.values():agent.subscribers.add(events.EventBuffer(8))
            with self.assertRaises(db.CommandCapacityError):self.agent()
            self.assertEqual(len(session.SESSIONS),3)

    def test_metrics_are_bounded_and_do_not_use_session_or_client_text_labels(self):
        with patch.object(telemetry,'_counts',Counter()),patch.object(telemetry,'_durations',{}):
            # Строки из внешнего события не входят в метки.
            telemetry.observe('operation',{'session':'private-id','text':'private-text','duration_ms':123,'name':'arbitrary'})
            report=telemetry.snapshot()
            self.assertNotIn('private',json.dumps(report))
            self.assertEqual(report['latency_ms']['operation']['count'],1)
            for i in range(1000):telemetry.count('metric-'+str(i))
            self.assertLessEqual(len(telemetry.snapshot()['counters']),telemetry.MAX_SERIES)


class PhoneScope(unittest.TestCase):
    def setUp(self):db.configure('sqlite://');session.SESSIONS.clear()

    def test_operator_grant_is_owned_single_use_and_tts_has_a_budget(self):
        cfg=replace(settings,phone_pin='operator-test',phone_tts_char_limit=12)
        with patch.object(db,'configure',return_value=db._engine),patch.object(main,'get_llm',return_value=ScriptedLLM()),patch.object(phone,'settings',cfg),patch.object(phone_access,'settings',cfg),\
             patch.object(phone,'credentials',AsyncMock(return_value={'username':'test','password':'opaque','realm':'test','ws':'wss://test'})),\
             patch.object(phone,'synthesize',AsyncMock(return_value=b'wav')) as synth,\
             TestClient(main.app,base_url='https://testserver') as owner,TestClient(main.app,base_url='https://testserver') as stranger:
            sid=owner.post('/api/session',json={'query':'Ищу квартиру'}).json()['id'];s=session.SESSIONS[sid]
            s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':9_000_000,'payment':'cash'})
            s.red_lines_ok=True;s.call_targets=['cian-1']
            s.active_operation={'id':'op','name':'call_seller','arguments':{'listing_id':'cian-1'},'status':'waiting'}
            s.pending_decisions={'call':{'status':'waiting','decision_id':'decision','operation_id':'op','payload':{'listing':listing()}}}
            s.remember()
            payload={'chat_id':sid,'decision_id':'decision','destination':'79991234567'}
            self.assertEqual(owner.post('/api/phone',json=payload).status_code,403)
            headers={'X-Phone-Pin':'operator-test'}
            self.assertEqual(stranger.post('/api/phone',json=payload,headers=headers).status_code,404)
            reply=owner.post('/api/phone',json=payload,headers=headers)
            self.assertEqual(reply.status_code,200,reply.text);token=reply.json()['grant_token']
            self.assertEqual(owner.post('/api/phone',json=payload,headers=headers).status_code,409)
            auth={**headers,'X-Phone-Grant':token}
            self.assertEqual(stranger.post('/api/tts',json={'text':'Привет'},headers=auth).status_code,403)
            self.assertEqual(owner.post('/api/tts',json={'text':'Привет'},headers=auth).status_code,200)
            self.assertEqual(owner.post('/api/tts',json={'text':'Длиннее лимита'},headers=auth).status_code,403)
            self.assertEqual(synth.await_count,1)
            owner.post('/api/phone/close',headers=auth)
            self.assertEqual(owner.post('/api/tts',json={'text':'Ещё'},headers=auth).status_code,403)

    def test_public_phone_without_operator_pin_is_closed(self):
        with patch.object(phone,'settings',replace(settings,phone_pin='')):
            self.assertFalse(phone.allowed(None));self.assertFalse(phone.allowed('anything'))
