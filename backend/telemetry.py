"""Ограниченные метрики процесса. Тексты, ID клиентов и параметры не являются метками."""
from collections import Counter, defaultdict
import time

BUCKETS=(10,50,100,250,500,1000,2500,5000,15000,60000,180000)
MAX_SERIES=128
_counts=Counter()
_durations={}
_dependencies={}
_started=time.monotonic()


def count(name,amount=1):
    if name not in _counts and len(_counts)>=MAX_SERIES-1:name='other'
    _counts[name]+=amount


def observe(event,fields):
    # Только фиксированные имена событий/исходов, без значений аргументов.
    allowed={'operation','turn','llm','llm_queue','request_review','request_review_invalid',
             'session_error','restored','persistence_failed','selection','sse_resync','sse_coalesced','command_rejected',
             'rule_parser','source_request'}
    if event not in allowed:return
    count(event)
    if event in ('request_review_invalid','persistence_failed','restored'):return
    if event=='selection':
        count('selection.confirmed',int(fields.get('confirmed',0)))
        count('selection.unverified',int(fields.get('unverified',0)))
    if event=='rule_parser':  # разбор кодом: accepted / declined / shadow_agree / shadow_disagree
        count('rule_parser.'+str(fields.get('outcome','unknown'))[:24])
    if event=='source_request':  # внешние запросы: cian_page / twogis / overpass / poi_local
        count('source.'+str(fields.get('source','unknown'))[:24])
    if event=='llm':
        if fields.get('tag'):count('llm.'+str(fields['tag'])[:32])  # сколько запросов к модели и каких
        ok=fields.get('outcome')=='ok'
        dependency('llm','available' if ok else 'degraded')
        if not ok:count('llm.failed')
    duration=fields.get('duration_ms')
    if isinstance(duration,(int,float)) and duration>=0:
        row=_durations.setdefault(event,{'count':0,'sum_ms':0,'buckets':[0]*(len(BUCKETS)+1)})
        row['count']+=1;row['sum_ms']+=duration
        index=next((i for i,b in enumerate(BUCKETS) if duration<=b),len(BUCKETS))
        row['buckets'][index]+=1


def dependency(name,status):
    if name in ('llm','database','map','listings','phone'):
        _dependencies[name]={'status':status,'observed_at':time.time()}


def dependencies():
    now=time.time()
    return {name:{**r,'age_seconds':round(now-r['observed_at'],1)} for name,r in _dependencies.items()}


def snapshot():
    return {'uptime_seconds':round(time.monotonic()-_started,1),'counters':dict(_counts),
            'latency_ms':{k:{**v,'bounds_ms':[*BUCKETS,None]} for k,v in _durations.items()},
            'dependencies':dependencies(),'scope':'one_process','series_limit':MAX_SERIES}
