"""Изменения профиля как список фактов, без десятков неназванных nullable-слотов."""
import json
from copy import deepcopy

from . import input_frame,selection_request,validation
from .request_schema import CHANGE_SCHEMA


def schema(contract):
    props=contract['properties']
    source=deepcopy(props['fields']['properties']['filters']['properties']['source'])
    source.pop('enum',None)  # любая буквальная подстрока; её проверяет decode
    def obj(fields):return {'type':'object','additionalProperties':False,'required':list(fields),'properties':fields}
    reading=deepcopy(selection_request.SCHEMA)
    reading['properties']['limit']['type']='integer';reading['properties']['limit']['minimum']=0
    reading['properties']['sort']['type']='array';reading['properties']['aggregates']['type']='array'
    reading['properties']['group_by']['type']='string'
    reading['properties']['group_by']['enum']=['',*input_frame.FIELDS]
    keys=list(dict.fromkeys([*input_frame.basic_fields(CHANGE_SCHEMA),*props['criteria']['properties']]))
    result=obj({
        'fields':{'type':'array','minItems':0,'maxItems':20,'items':obj({
            'key':{'type':'string','enum':keys},
            'value':{'type':'string','maxLength':3000,'description':'Строковое поле — обычная строка; число/boolean/массив — JSON-значение, например [1,2], 30000000, true.'},
            'source':source})},
        'criteria':{'type':'array','minItems':0,'maxItems':30,'items':obj({
            'key':{'type':'string','enum':keys},
            'priority':{'type':'string','enum':['required','preferred','remove']},
            'value':{'type':'string','maxLength':1000,'description':'Значение в формате поля; для poi/feature и запрета этажей true; для remove пустая строка.'},
            'source':source})},
        'places':deepcopy(props['places']),
        'unsupported':deepcopy(props['unsupported']),
        'selection':{'type':'array','minItems':0,'maxItems':1,'items':reading,
                     'description':'[] если команды отбора нет. Иначе один запрос; limit0 — сохранить количество; group_by пусто — не группировать.'},
    })
    for key in ('places','unsupported'):result['properties'][key]['minItems']=0
    result['properties']['fields']['items']['properties']['action']={'type':'string','enum':['set','remove']}
    return result


def value(text,spec):
    if spec['type']=='string':return text
    return json.loads(text)


def decode(patch,contract,inputs):
    patch=deepcopy(patch)
    for rows in (patch.get('fields') or [],patch.get('criteria') or []):
        for row in rows:
            if 'value' not in row and (row.get('action')=='remove' or row.get('priority')=='remove'):row['value']=''
            if 'value' in row and not isinstance(row['value'],str):row['value']=json.dumps(row['value'],ensure_ascii=False)
    validation.validate(patch,contract)
    frame={'fields':{},'criteria':{},'places':patch['places'],'unsupported':patch['unsupported']}
    known=input_frame.basic_fields(CHANGE_SCHEMA)
    texts=[r['text'].casefold() for r in inputs]
    for rows in (patch['fields'],patch['criteria']):
        for row in rows:
            if not row['source'] or not any(row['source'].casefold() in text for text in texts):
                raise ValueError('Каждое изменение требует буквальной цитаты нового ввода')
    def put(group,key,item):
        old=frame[group].get(key)
        if old is None:frame[group][key]=item;return
        if group=='fields':
            a,b=old['value'],item['value']
            if isinstance(a,list) and isinstance(b,list):
                if key=='filters':
                    merged={f['field']:f for f in a}
                    for f in b:
                        if f['field'] in merged and merged[f['field']]!=f:
                            raise ValueError('Противоречащие фильтры свойства '+f['field'])
                        merged[f['field']]=f
                    old['value']=list(merged.values())
                else:old['value']=list(dict.fromkeys([*a,*b]))
                return
            if a==b:return
        elif old==item:return
        raise ValueError('Противоречащие значения свойства '+key)
    for row in [*patch['fields'],*patch['criteria']]:
        key,priority=row['key'],row.get('priority','required')
        if row.get('action')=='remove' or row['value'].strip() in ('null',''):priority='remove'
        if key in known:
            if priority=='remove':
                kind=known[key]['type']
                if kind not in ('boolean','array'):raise ValueError('Для изменения '+key+' нужно новое значение')
                actual=False if kind=='boolean' else []
            else:actual=value(row['value'],known[key])
            put('fields',key,{'value':actual,'source':row['source']})
            continue
        if key.startswith(('poi:','feature:')) or key in ('not_first_floor','not_last_floor'):
            put('criteria',key,priority)
        else:
            put('criteria',key,{'value':None if priority=='remove' else value(row['value'],input_frame.criterion_value(key,CHANGE_SCHEMA)),
                               'priority':priority})
    if patch.get('selection'):
        q=deepcopy(patch['selection'][0]);q['limit']=q['limit'] or None;q['group_by']=q['group_by'] or None
        frame['selection']=q
    return frame
