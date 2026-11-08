"""Ограниченный контекст; полные данные и история остаются в /input/data.json."""
from copy import deepcopy
import json

HISTORY_BYTES=4200
SUMMARY_BYTES=6500


def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def size(value):
    return len(encoded(value).encode('utf-8'))


def excerpt(text,budget):
    text=str(text or '')
    raw=text.encode('utf-8')
    if len(raw)<=budget:return text
    marker='\n[Сокращено; полный текст в /input/data.json]\n'
    available=max(0,budget-len(marker.encode('utf-8')))
    head=available*3//4;tail=available-head
    return raw[:head].decode('utf-8',errors='ignore')+marker+(raw[-tail:].decode('utf-8',errors='ignore') if tail else '')


def context_messages(data,max_bytes=HISTORY_BYTES):
    result=[]
    for message in reversed(data.get('conversation',[])[-8:]):
        role=message.get('role')
        if role not in ('user','assistant'):continue
        remaining=max_bytes-size(result)-60
        if remaining<120:break
        text=excerpt(message.get('content'),min(1600 if role=='user' else 1200,remaining))
        candidate=[{'role':role,'content':text},*result]
        if size(candidate)<=max_bytes:result=candidate
    return result


def facts_context(facts,max_bytes=4200):
    """Небольшие метрики целиком; большие таблицы читаются кодом из файла."""
    if size(facts)<=max_bytes:return deepcopy(facts)
    result={k:deepcopy(v) for k,v in facts.items()
            if type(v) in (int,float,bool) or v is None or isinstance(v,str) and len(v)<=120}
    result['overview_only']=True
    result['details_file']='/input/data.json'
    for key in ('rows','groups'):
        rows=facts.get(key)
        if not isinstance(rows,list):continue
        result[key+'_count']=len(rows)
        result[key+'_path']='facts.'+key
        if key=='groups' and size({**result,key:rows})<=max_bytes:result[key]=deepcopy(rows)
    if size(result)>max_bytes:
        result={k:result[k] for k in ('kind','version','count','median_price','median_price_per_m2',
            'overview_only','details_file','rows_count','rows_path','groups_count','groups_path') if k in result}
    return result


def initial_task(data):
    return data['prompt']+'\nПолные данные и проверенные метрики: /input/data.json. Ниже только обзор; для анализа всех квартир читай файл через Python.\n'+encoded(facts_context(data['facts']))


def summary(data,messages,successful_runs,photos=None):
    payload={'task':data['prompt'],'kind':data['kind'],
        'facts':facts_context(data['facts'],max_bytes=max(400,4000-size(data['prompt']))),
        'successful_python_runs':successful_runs,
        'files':'Полные данные, метрики и история: /input/data.json. Скрипты и отчёты: /workspace.',
        'photo_results':'/workspace/photo-analysis.json содержит полные результаты просмотра фото.',
        'next':'Не повторяй завершённые расчёты. Прочитай только недостающие данные и ответь на текущий вопрос.'}
    def append(key,value):
        candidate={**payload,key:value}
        if size(candidate)<=SUMMARY_BYTES:payload[key]=value;return True
        return False
    visual=[]
    for row in list(photos if photos is not None else data.get('visual_analysis',[]))[-3:]:
        record={k:excerpt(row.get(k),180) if isinstance(row.get(k),str) else row.get(k)
                for k in ('listing_id','address','status','visible_state','image_kind','note')}
        record.update(photos_seen=len(row.get('photos',[])),
            observations=[excerpt(x,350) for x in row.get('observations',[])[:4]],
            limitations=[excerpt(x,300) for x in row.get('limitations',[])[:2]])
        if append('visual_analysis',visual+[record]):visual.append(record)
    observations=[excerpt(m.get('content'),600) for m in messages if m.get('role')=='tool'][-3:]
    append('last_observations',observations)
    append('conversation',context_messages(data,max_bytes=min(1600,max(0,SUMMARY_BYTES-size(payload)-30))))
    return encoded(payload)
