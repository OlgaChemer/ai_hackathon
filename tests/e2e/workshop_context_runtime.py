"""Настоящий Docker/Nanobot, модель подменена: переполнение и сжатие истории без платных запросов.

Запуск на сервере из корня проекта: python -m tests.e2e.workshop_context_runtime
  --image mesto-workshop:0.4.2 --input /private/path/input.json
  [--previous-image sha256:...]
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from backend.llm import LLMResponse,ToolCall
from backend.workshop import sandbox


class Model:
    name='fake';model='context-runtime-test'
    def __init__(self):self.calls=0

    async def chat(self,messages,**kw):
        self.calls+=1
        if self.calls<=4:
            code="""import json,statistics
d=json.load(open('/input/data.json'))
prices=[x['price'] for x in d['listings']]
print(json.dumps({'count':len(prices),'median':statistics.median(prices)}))
print('р'*3990)
"""
            return LLMResponse('',[ToolCall('run_python',{'code':code},'context-'+str(self.calls))])
        return LLMResponse('Исследование недвижимости завершено. Расчёты и исходная выборка сохранены в Excel.')


async def check(args):
    snapshot=json.loads(Path(args.input).read_text())
    assert len(snapshot['listings'])==150
    previous=sandbox.settings
    with tempfile.TemporaryDirectory(prefix='workshop-context-test-') as directory:
        async def execute(image,job):
            sandbox.settings=replace(previous,workshop_image=image,workshop_root=directory)
            events=[];model=Model()
            result=await sandbox.run(job,snapshot,model,events.append)
            assert model.calls==5
            assert result['facts']==snapshot['facts']
            assert result['code_runs']==4
            assert any('Сохраняю выводы' in text for text in events),events
            with zipfile.ZipFile(Path(directory)/job/'artifacts/analysis.xlsx') as book:
                rows=ET.fromstring(book.read('xl/worksheets/sheet2.xml')).findall('.//{*}row')
                assert len(rows)==151
            return {'calls':model.calls,'code_runs':result['code_runs'],'source_rows':150,
                    'excel_source_rows':len(rows)-1,'compaction':True}
        try:
            if args.previous_image:
                try:await execute(args.previous_image,'old-image')
                except RuntimeError as exc:assert 'ContextWindowExceeded' in str(exc),str(exc)
                else:raise AssertionError('The old image did not reproduce the context failure')
                print('PASS old image: original context failure reproduced',flush=True)
            result=await execute(args.image,'new-image')
            print(json.dumps({'passed':True,**result},ensure_ascii=False),flush=True)
        finally:sandbox.settings=previous


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    parser.add_argument('--input',required=True);parser.add_argument('--previous-image')
    asyncio.run(check(parser.parse_args()))
