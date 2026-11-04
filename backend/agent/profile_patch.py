"""Изменения профиля как список фактов, без десятков неназванных nullable-слотов."""
import json
from copy import deepcopy

from . import input_frame,selection_request,validation
from .request_schema import CHANGE_SCHEMA


def schema(contract):
    props=contract['properties']
    source=deepcopy(props['fields']['properties']['filters']['properties']['source'])
    def obj(fields):return {'type':'object','additionalProperties':False,'required':list(fields),'properties':fields}
    reading=deepcopy(selection_request.SCHEMA)
    reading['properties']['limit']['type']='integer';reading['properties']['limit']['minimum']=0
    reading['properties']['sort']['type']='array';reading['properties']['aggregates']['type']='array'
    reading['properties']['group_by']['type']='string'
    reading['properties']['group_by']['enum']=['',*input_frame.FIELDS]
    result=obj({
        'fields':{'type':'array','minItems':0,'maxItems':20,'items':obj({
            'key':{'type':'string','enum':list(input_frame.basic_fields(CHANGE_SCHEMA))},
            'value':{'type':'string','maxLength':3000,'description':'Строковое поле — обычная строка; число/boolean/массив — JSON-значение, например [1,2], 30000000, true.'},
            'source':source})},
        'criteria':{'type':'array','minItems':0,'maxItems':30,'items':obj({
            'key':{'type':'string','enum':list(props['criteria']['properties'])},
            'priority':{'type':'string','enum':['required','preferred','remove']},
            'value':{'type':'string','maxLength':1000,'description':'Значение в формате поля; для poi/feature и запрета этажей true; для remove пустая строка.'},
            'source':source})},
        'places':deepcopy(props['places']),
        'unsupported':deepcopy(props['unsupported']),
        'selection':{'type':'array','minItems':0,'maxItems':1,'items':reading,
                     'description':'[] если команды отбора нет. Иначе один запрос; limit0 — сохранить количество; group_by пусто — не группировать.'},
    })
    for key in ('places','unsupported'):result['properties'][key]['minItems']=0
    return result


def value(text,spec):
    if spec['type']=='string':return text
    return json.loads(text)


def decode(patch,contract,inputs):
    validation.validate(patch,contract)
    frame={'fields':{},'criteria':{},'places':patch['places'],'unsupported':patch['unsupported']}
    known=input_frame.basic_fields(CHANGE_SCHEMA)
    texts=[r['text'].casefold() for r in inputs]
    for rows in (patch['fields'],patch['criteria']):
        seen=set()
        for row in rows:
            if row['key'] in seen:raise ValueError('Повтор одного свойства в списке изменений')
            seen.add(row['key'])
            if not row['source'] or not any(row['source'].casefold() in text for text in texts):
                raise ValueError('Каждое изменение требует буквальной цитаты нового ввода')
    for row in patch['fields']:
        frame['fields'][row['key']]={'value':value(row['value'],known[row['key']]),'source':row['source']}
    for row in patch['criteria']:
        key,priority=row['key'],row['priority']
        if key.startswith(('poi:','feature:')) or key in ('not_first_floor','not_last_floor'):
            frame['criteria'][key]=priority
        else:
            frame['criteria'][key]={'value':None if priority=='remove' else value(row['value'],input_frame.criterion_value(key,CHANGE_SCHEMA)),
                                     'priority':priority}
    if patch.get('selection'):
        q=deepcopy(patch['selection'][0]);q['limit']=q['limit'] or None;q['group_by']=q['group_by'] or None
        frame['selection']=q
    return frame
