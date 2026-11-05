"""Повторяемый критерий выпуска. Настоящая модель запускается только с --live."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from backend.release import manifest


def main():
    p=argparse.ArgumentParser();p.add_argument('--report-dir',required=True)
    p.add_argument('--node',default=shutil.which('node'));p.add_argument('--live',action='store_true')
    p.add_argument('--repeat',type=int,default=2);p.add_argument('--provider',choices=['gigachat','deepseek'])
    args=p.parse_args()
    if not 1<=args.repeat<=5:p.error('repeat must be 1..5')
    directory=Path(args.report_dir).resolve();directory.mkdir(parents=True,exist_ok=True)
    steps=[('python',[sys.executable,'-m','unittest','discover','-s','tests','-q'],120),
           ('javascript',[args.node or 'node','--test',*map(str,sorted((ROOT/'tests/frontend').glob('*.test.mjs')))],60),
           ('load',[sys.executable,'-m','tests.e2e.load_state','--report',str(directory/'load.json')],90)]
    if args.live:
        for dataset,repeats in [('regression',args.repeat),('heldout',1)]:
            cmd=[sys.executable,'-m','tests.e2e.semantic_eval','--dataset',dataset,'--repeat',str(repeats),'--report',str(directory/(dataset+'.json'))]
            if args.provider:cmd+=['--provider',args.provider]
            steps.append((dataset,cmd,600))
    outcomes=[]
    for name,cmd,timeout in steps:
        started=time.monotonic()
        try:
            with (directory/(name+'.log')).open('w') as output:
                completed=subprocess.run(cmd,cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
            code=completed.returncode
        except (OSError,subprocess.TimeoutExpired):code=124
        outcomes.append({'step':name,'exit_code':code,'seconds':round(time.monotonic()-started,2)})
        print(json.dumps(outcomes[-1]),flush=True)
    good=all(item['exit_code']==0 for item in outcomes)
    (directory/'gate.json').write_text(json.dumps({'passed':good,'manifest':manifest(),
        'created_at':datetime.now(timezone.utc).isoformat(),'steps':outcomes,
        'model_exam':args.live,'real_telephone_calls':False},ensure_ascii=False,indent=2))
    return 0 if good else 1


if __name__=='__main__':raise SystemExit(main())
