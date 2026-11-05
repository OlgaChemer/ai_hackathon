"""Нагрузка на SQLite/команды без внешней модели, пользовательской БД и телефонии."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import tempfile
import time
from backend import db
from backend.agent import session
from backend.agent.profile import merge_profile
from backend.release import manifest
from tests.fakes import ScriptedLLM


async def run(report_path=None):
    with tempfile.TemporaryDirectory(prefix='agent-load-') as folder:
        db.configure('sqlite:///'+folder+'/load.db')
        agents=[]
        for _ in range(8):
            s=session.create_session('',ScriptedLLM())
            s.profile=merge_profile({'city':'Казань','rooms':2,'budget_max':9_000_000,'payment':'cash'})
            s.prefs.sync(s.profile);s.remember();agents.append(s)
        latency=[];max_queue=0;started=time.monotonic()
        async def command(s,index):
            t=time.monotonic()
            result=await s.receive_text('Продолжить',command_id=f'load-{index}')
            latency.append((time.monotonic()-t)*1000)
            assert result=='message',result
        tasks=[asyncio.create_task(command(s,i)) for s in agents for i in range(16)]
        while not all(t.done() for t in tasks):
            max_queue=max(max_queue,max(s.command_queue.qsize() for s in agents))
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        effects=sum(sum(e['type']=='message' and e.get('role')=='user' for e in s.history) for s in agents)
        assert effects==128,effects
        assert all(not s.command_futures for s in agents)
        result={'manifest':manifest(agents[0].llm),'scope':'SQLite and actor only, no model latency',
                'dialogues':8,'requests':128,'effects':effects,'max_queue':max_queue,
                'seconds':round(time.monotonic()-started,3),'p50_ms':round(statistics.median(latency),1),
                'p95_ms':round(sorted(latency)[int(len(latency)*.95)-1],1)}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if report_path:Path(report_path).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        for s in agents:session.SESSIONS.pop(s.id,None)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report');args=p.parse_args()
    asyncio.run(run(args.report))
