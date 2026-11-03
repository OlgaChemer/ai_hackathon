"""Типизированный ввод: модель понимает речь, код связывает условие с его приоритетом."""
from copy import deepcopy

from . import validation
from .criteria import CRITERIA_KEYS, SCALARS
from .features import FEATURES
from .. import geo

EXCLUDED = {'city','cities','features','poi','soft_criteria','clear_criteria', *SCALARS}
INTENTS = ['search','refine','question','continue','hold','other']
SOURCE = {'type':'integer','minimum':1}


def basic_fields(changes):
    fields={k:deepcopy(v) for k,v in changes['properties'].items() if k not in EXCLUDED}
    # У комнат до пяти уже есть множество вариантов. Второе скалярное представление
    # побуждает модель выдавать несколько противоречащих записей для одного диапазона.
    fields['rooms']['minimum']=6
    fields['rooms']['description']='Один вариант комнатности от 6. Для 0–5 используй rooms_options, даже для одного числа.'
    fields['rooms_options']['description']='Все допустимые числа жилых комнат. 0 — только студия, 1 — однокомнатная, 2 — двухкомнатная и т.д. Неизвестность — null, не 0. Не сужай диапазон или перечисление.'
    return fields


def criterion_value(key, changes):
    if key.startswith(('poi:', 'feature:')):
        return {'type':'boolean','enum':[True]}
    return deepcopy(changes['properties'][key])


def object_schema(properties):
    return {'type':'object','additionalProperties':False,'required':list(properties),'properties':properties}


def latest_rows(rows, key):
    """Повтор факта допустим; разные сообщения применяются по порядку, конфликт одного сообщения — нет."""
    result={}
    for row in rows:
        previous=result.get(row[key])
        if previous:
            def comparable(item):
                return {k:sorted(v,key=str) if isinstance(v,list) else v for k,v in item.items() if k!='source_seq'}
            if row['source_seq']==previous['source_seq'] and comparable(row)!=comparable(previous):
                raise ValueError('Противоречащие значения поля '+str(row[key])+' требуют уточнения')
            if row['source_seq'] < previous['source_seq']:continue
        result[row[key]]=row
    return list(result.values())


def nullable(value):
    value=deepcopy(value)
    value['type']=[value['type'],'null']
    if 'enum' in value:value['enum'].append(None)
    return value


def schema(changes):
    priorities={'type':'string','enum':['required','preferred','remove','unmentioned']}
    criteria={}
    labels={**SCALARS,**{f'feature:{k}':v[0] for k,v in FEATURES.items()},**{f'poi:{k}':v[0] for k,v in geo.POI.items()}}
    for key in CRITERIA_KEYS:
        if key.startswith(('poi:','feature:')) or key in ('not_first_floor','not_last_floor'):
            criteria[key]=nullable({'type':'string','enum':['required','preferred','remove'],
                'description':'Обязательное / желательное / снять условие. null — не названо.'})
        else:
            criteria[key]=nullable(object_schema({'value':nullable(criterion_value(key,changes)),
                'priority':priorities}))
        criteria[key]['description']=labels[key]+'. Не названо клиентом — null. Без предположительных значений.'
    return object_schema({
        'places':{'type':'array','maxItems':3,'description':'Только названные итоговые территории поиска. Если не менялись — пустой список.',
            'items':object_schema({'words':{'type':'array','minItems':1,'maxItems':8,'items':object_schema({
                'source':{'type':'string','maxLength':80,'description':'Одно исходное слово названия, без предлогов.'},
                'lemma':{'type':'string','maxLength':80,'description':'Начальная форма этого слова. Не добавляй отсутствующие слова.'}})}})},
        'fields':object_schema({key:nullable(value) for key,value in basic_fields(changes).items()}),
        'criteria':object_schema(criteria),
        'question':{'type':'string','maxLength':500,'description':'Один вопрос только при неясности, иначе пустая строка.'},
        'options':{'type':'array','maxItems':5,'items':{'type':'string','maxLength':100}},
        'intent':{'type':'string','enum':INTENTS,'description':'question — информационный вопрос; refine — изменить условия; hold — пока не искать; search — начать; continue — продолжить; other — остальное.'},
    })


def decode(frame, profile, payload, changes_schema):
    """Преобразует машинные поля; свободный текст здесь не разбирается."""
    native=isinstance(frame.get('fields'),dict)
    native_schema=schema(changes_schema)
    current=payload['messages'][-1]['seq']
    if native:
        validation.validate(frame['fields'],native_schema['properties']['fields'])
        raw_criteria=frame.get('criteria')
        validation.validate(raw_criteria,native_schema['properties']['criteria'])
        fields=[{'field':k,'value':v,'source_seq':current} for k,v in frame['fields'].items() if v is not None]
        criteria=[]
        for key,value in raw_criteria.items():
            if value is None:continue
            if isinstance(value,str):
                priority=value; actual=None if priority=='remove' else True
            else:
                priority=value['priority'];actual=value['value']
            if priority=='unmentioned':continue
            criteria.append({'key':key,'value':actual,'priority':priority,'source_seq':current})
        frame={**frame,'fields':fields,'criteria':criteria,
               'places':[{**row,'source_seq':current} for row in frame['places']]}
    envelope={k:v for k,v in frame.items() if k not in ('fields','criteria','places')}
    envelope_schema={**native_schema,'properties':{k:v for k,v in native_schema['properties'].items() if k not in ('fields','criteria','places')}}
    envelope_schema['required']=list(envelope_schema['properties'])
    validation.validate(envelope,envelope_schema)
    place_schema=deepcopy(native_schema['properties']['places'])
    place_schema['items']['properties']['source_seq']=SOURCE
    place_schema['items']['required'].append('source_seq')
    validation.validate(frame.get('places'),place_schema)
    if frame['intent']=='question':
        return {'intent':'question','decision':'unchanged','changes':{},'evidence':[], 'question':'','options':[]}
    seqs = {x['seq'] for x in payload['messages']}
    def source(row):
        if row['source_seq'] not in seqs:
            raise ValueError('Основание не ссылается на принятый ввод')
        return row['source_seq']
    changes, evidence = {}, {}
    def put(key,value,seq):
        changes[key]=value
        evidence[key]={'field':key,'source_seq':seq,'quote':''}
    for key in ('fields','criteria'):
        if not isinstance(frame.get(key),list) or len(frame[key]) > 64:
            raise ValueError('Неверный список параметров')
    fields = basic_fields(changes_schema)
    for row in frame['fields']:
        if not isinstance(row,dict) or row.get('field') not in fields:
            raise ValueError('Неизвестное поле запроса')
        field=row['field']
        validation.validate(row,object_schema({'field':{'type':'string','enum':[field]},'value':fields[field],'source_seq':SOURCE}))
        source(row)
    for row in latest_rows(frame['fields'],'field'):
        put(row['field'],row['value'],row['source_seq'])
    soft=set(profile.get('soft_criteria') or [])
    lists={key:list(profile.get(key) or []) for key in ('poi','features')}
    for row in frame['criteria']:
        if not isinstance(row,dict) or row.get('key') not in CRITERIA_KEYS:
            raise ValueError('Неизвестное условие')
        key=row['key']; removing=row.get('priority')=='remove'
        value_schema={'type':['string','null']} if removing else criterion_value(key,changes_schema)
        # При снятии условия значение не используется: null не должен превращаться в догадку.
        if removing and row.get('value') is not None:
            raise ValueError('Снятие условия требует value=null')
        validation.validate(row,object_schema({'key':{'type':'string','enum':[key]},'value':value_schema,
            'priority':{'type':'string','enum':['remove'] if removing else ['required','preferred']},'source_seq':SOURCE}))
        source(row)
    for row in latest_rows(frame['criteria'],'key'):
        key=row['key'];removing=row['priority']=='remove';seq=row['source_seq']
        soft.discard(key)
        if removing:
            put('clear_criteria',[*(changes.get('clear_criteria') or []),key],seq)
        elif row['priority']=='preferred':
            soft.add(key)
        if key.startswith(('poi:','feature:')):
            group='poi' if key.startswith('poi:') else 'features'
            name=key.split(':',1)[1]
            lists[group]=[x for x in lists[group] if x!=name]
            if not removing:lists[group].append(name)
            put(group,lists[group],seq)
        elif not removing:
            put(key,row['value'],seq)
        put('soft_criteria',sorted(soft),seq)
    canonical={x.casefold():x for x in payload['supported_cities']}
    word_sets={tuple(sorted(x.casefold().split())):x for x in payload['supported_cities']}
    ambiguous={k.casefold():v for k,v in payload['ambiguous_catalog_names'].items()}
    choices=[x for x in payload['messages'][-1].get('options',[]) if x in payload['supported_cities']]
    places=[]
    question,options=frame['question'],frame['options']
    for row in frame['places']:
        seq=source(row)
        text=' '.join(x['text'] for x in payload['messages'] if native or x['seq']==seq).casefold()
        if not row['words']:raise ValueError('Нужно исходное название территории')
        seen_words=set();valid=True
        for word in row['words']:
            raw=word['source'].strip().casefold();lemma=word['lemma'].strip()
            if not raw or raw not in text or raw in seen_words or not lemma or any(c.isspace() for c in lemma):valid=False
            seen_words.add(raw)
        name=' '.join(w['lemma'].strip() for w in row['words']); normalized=name.casefold()
        matched=canonical.get(normalized) or word_sets.get(tuple(sorted(normalized.split())))
        if not matched and choices:
            words=set(normalized.split())
            candidates=[x for x in choices if words <= set(x.casefold().split())]
            if len(candidates)==1:matched=candidates[0]
        if not valid:
            if native and matched in {profile.get('city'),*(profile.get('cities') or [])}:continue
            raise ValueError('Каждому исходному слову названия нужна одна начальная форма, без добавления слов')
        if matched:
            places.append(matched)
        elif normalized in ambiguous:
            options=ambiguous[normalized]
            question='Какой город вы имеете в виду: ' + ' или '.join(options) + '?'
        else:
            # Это справочник синонимов выделенного моделью топонима, не разбор запроса.
            from ..sources import normalize_city
            alias=normalize_city(name)
            if alias in payload['supported_cities']:
                places.append(alias)
            else:
                question=f'Города «{name}» пока нет среди поддерживаемых. В каком городе искать?'
                options=[]
    if places:
        seq=frame['places'][0]['source_seq']
        places=list(dict.fromkeys(places))
        put('city',places[0],seq)
        put('cities',places[1:],seq)
    if question.strip():
        return {'intent':frame['intent'],'decision':'clarify','changes':{},'evidence':[], 'question':question,'options':options}
    return {'intent':frame['intent'],'decision':'apply' if changes else 'unchanged', 'changes':changes,
            'evidence':list(evidence.values()),'question':'','options':options}
