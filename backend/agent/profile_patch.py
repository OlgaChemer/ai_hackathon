"""Изменения профиля как список фактов, без десятков неназванных nullable-слотов."""
import json
from copy import deepcopy

from . import input_frame,selection_request,validation
from .request_schema import CHANGE_SCHEMA

QUESTIONNAIRES=['ask_housing','ask_payment','ask_family','confirm_red_lines']


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
    keys=list(dict.fromkeys([*input_frame.basic_fields(CHANGE_SCHEMA),*props['criteria']['properties'],'city','cities','unsupported']))
    key={'type':'string','enum':keys,
         'description':'Точный известный ключ либо unsupported для отсутствующего свойства. Не заменяй неизвестное ближайшим известным.'}
    result=obj({
        'filters':{'type':'array','minItems':0,'maxItems':20,
            'description':'Только явно добавленные/снятые ограничения квартиры. Для сортировки, количества и сравнения — []. Не добавлять проверки качества данных: price/area/price_per_m2 > 0, exists. Число карточек не порог свойства.', 'items':{
            **deepcopy(CHANGE_SCHEMA['properties']['filters']['items']),
            'properties':{**deepcopy(CHANGE_SCHEMA['properties']['filters']['items']['properties']),'source':deepcopy(source)},
            'required':[*CHANGE_SCHEMA['properties']['filters']['items']['required'],'source']}},
        'fields':{'type':'array','minItems':0,'maxItems':20,'items':obj({
            'key':deepcopy(key),
            'value':{'type':'string','maxLength':3000,'description':'Строковое поле — обычная строка; число/boolean/массив — JSON-значение, например [1,2], 30000000, true.'},
            'source':source})},
        'criteria':{'type':'array','minItems':0,'maxItems':30,'items':obj({
            'key':deepcopy(key),
            'priority':{'type':'string','enum':['required','preferred','remove']},
            'value':{'type':'string','maxLength':1000,'description':'Значение в формате поля; для poi/feature и запрета этажей true; для remove пустая строка.'},
            'source':source})},
        'states':{'type':'array','minItems':0,'maxItems':5,'items':obj({
            'key':{'type':'string','enum':['budget_max','rooms','districts','okrug','housing']},
            'state':{'type':'string','enum':['unknown','unrestricted']},'source':deepcopy(source)})},
        'places':deepcopy(props['places']),
        'unsupported':deepcopy(props['unsupported']),
        'withdraw_unsupported':{'type':'array','minItems':0,'maxItems':5,'items':obj({
            'text':{'type':'string','maxLength':200,'description':'Точное сохранённое пожелание из предложенного enum, без перефразирования.'},
            'source':deepcopy(source)})},
        'selection':{'type':'array','minItems':0,'maxItems':1,'items':reading,
                     'description':'[] если команды отбора нет. Иначе один запрос; limit0 — сохранить количество; group_by пусто — не группировать.'},
        'requested_card':{'type':'array','minItems':0,'maxItems':1,'items':{'type':'string','enum':QUESTIONNAIRES},
                          'description':'Только явно запрошенная анкетная карточка. Где искать/рынок -> ask_housing; оплата -> ask_payment; семья -> ask_family; лимит -> confirm_red_lines. Иначе [].'},
    })
    for key in ('places','unsupported'):result['properties'][key]['minItems']=0
    result['properties']['unsupported']['items']['properties']['source'].pop('enum',None)
    result['properties']['fields']['items']['properties']['action']={'type':'string','enum':['set','remove']}
    return result


def value(text,spec):
    if spec['type']=='string':
        try:parsed=json.loads(text)
        except ValueError:return text
        return parsed if isinstance(parsed,str) else text
    parsed=json.loads(text)
    if isinstance(parsed,str):
        try:return json.loads(parsed)
        except ValueError:pass
    return parsed


def generation_schema(contract,inputs,unsupported=None):
    """Нативный вывод выбирает цитату из нового ввода, а не склоняет её заново."""
    result=deepcopy(contract)
    # Целая новая реплика содержит и значение, и его предмет. Один предлог
    # из той же реплики формально буквален, но не доказывает город или сумму.
    quotes=list(dict.fromkeys(row['text'] for row in inputs))
    for group in ('fields','criteria','unsupported','states','filters','withdraw_unsupported'):
        spec=result['properties'][group]['items']['properties']['source']
        spec['enum']=quotes
        spec['maxLength']=max([1000,*map(len,quotes)])
    for group in ('fields','criteria'):
        keys=result['properties'][group]['items']['properties']['key']['enum']
        result['properties'][group]['items']['properties']['key']['enum']=[key for key in keys if key not in ('field_states','filters','unsupported')]
    known=list(dict.fromkeys(unsupported or []))
    withdrawals=result['properties']['withdraw_unsupported']
    withdrawals['maxItems']=min(5,len(known))
    withdrawals['items']['properties']['text']['enum']=known or ['']
    result['properties']['unsupported']['items']['properties']['action']['enum']=['add']
    return result


def unverifiable_distance(f):
    if not isinstance(f,dict) or not isinstance(f.get('field'),str):return False
    if not f['field'].startswith('nearby:') or f.get('priority')=='remove':return False
    if not f.get('values'):return False
    if f['values'] in (['true'],['false']):return True
    try:return all(float(v)==0 for v in f['values'])
    except (ValueError,TypeError):return False


def decode(patch,contract,inputs,aliases=None,*,provenance=None,profile=None,reading=None):
    if not isinstance(patch,dict):raise ValueError('Изменения должны быть JSON-объектом')
    patch=deepcopy(patch)
    if 'selection' not in contract['properties'] and patch.get('selection') in (None,[]):
        patch.pop('selection',None)  # пустой слот старого формата не является командой
    patch.setdefault('states',[])
    patch.setdefault('filters',[])
    patch.setdefault('withdraw_unsupported',[])
    for group in ('fields','criteria','filters','states','withdraw_unsupported'):
        rows=patch.get(group,[])
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise ValueError(group+' должен быть списком объектов')
        key='field' if group=='filters' else 'key'
        if any(not isinstance(row.get(key,''),str) or not isinstance(row.get('source',''),str) for row in rows):
            raise ValueError(group+': имя свойства и цитата должны быть строками')
    def mode_for(key,state,quote):
        from .clarification import ground_state
        context=next((row for row in reversed(inputs) if quote.casefold() in row['text'].casefold()),{})
        return ground_state(key,state,quote,bound_field=context.get('question_field'),question=context.get('question',''))
    fields=[]
    for row in patch.get('fields',[]):
        key='rooms' if row.get('key')=='rooms_options' else row.get('key')
        raw=row.get('value')
        if key in ('budget_max','rooms','districts','okrug','housing') and raw in ('unknown','unrestricted'):
            mode=mode_for(key,raw,row['source'])
            if mode is None:raise ValueError('Статус '+key+' требует явного решения по этому свойству')
            if not any(s['key']==key and s['source']==row['source'] for s in patch['states']):
                patch['states'].append({'key':key,'state':mode,'source':row['source']})
        else:fields.append(row)
    patch['fields']=fields
    if reading is not None:
        from .clarification import names_property
        def room_fact(row):
            quote=row.get('source','')
            context=next((r for r in reversed(inputs) if quote.casefold() in r['text'].casefold()),{})
            return names_property('rooms',quote,bound_field=context.get('question_field'),question=context.get('question',''))
        # Число в уже принятой команде выдачи не доказывает комнатность.
        # Составная команда с явным упоминанием комнат или ответ на адресованный
        # вопрос по-прежнему может изменить профиль вместе с выдачей.
        for group in ('fields','criteria'):
            patch[group]=[r for r in patch.get(group,[]) if r.get('key') not in ('rooms','rooms_options','studio_requested') or room_fact(r)]
        patch['filters']=[r for r in patch['filters'] if r.get('field')!='rooms' or room_fact(r)]
        patch['states']=[r for r in patch['states'] if r.get('key')!='rooms' or room_fact(r)]
    from .conditions import ALIASES,compile_profile
    from .listing_query import _typed
    known=input_frame.basic_fields(CHANGE_SCHEMA)
    named=[]
    for row in [*patch.get('fields',[]),*patch.get('criteria',[])]:
        key=row.get('key','')
        if key not in ALIASES and not key.startswith(('poi:','feature:')):continue
        if row.get('action')=='remove' or row.get('priority')=='remove':continue
        raw=row.get('value')
        if raw in (None,'','null'):continue
        spec=known.get(key) or input_frame.criterion_value(key,CHANGE_SCHEMA)
        actual=value(raw if isinstance(raw,str) else json.dumps(raw),spec)
        if actual in (None,False,[]):continue
        p={'soft_criteria':[key] if row.get('priority')=='preferred' else []}
        if key.startswith(('poi:','feature:')):
            p['poi' if key.startswith('poi:') else 'features']=[key.split(':',1)[1]]
        else:p[key]=actual
        named.extend((row.get('source'),c) for c in compile_profile(p))
    # Нативный контракт передаёт сравнения объектами, без JSON внутри строки.
    # Декодер старых сохранённых результатов по-прежнему поддерживается.
    for item in patch['filters']:
        quote=item.get('source','')
        if any(source==quote and all(item.get(k)==c[k] for k in ('field','op','priority'))
               and [_typed(v,item['field']) for v in item['values']]==c['values'] for source,c in named):
            # Тот же смысл уже пришёл именованным условием. Радиусы центра и
            # инфраструктуры берутся из общей политики, а не из новой цитаты.
            # Отличающийся порог остаётся самостоятельным ограничением.
            continue
        if item.get('field')=='price' and item.get('op')=='lte' and item.get('priority')=='required' and len(item['values'])==1:
            amount=float(item['values'][0])
            if amount.is_integer() and 1<=amount<=1_000_000_000:
                patch.setdefault('fields',[]).append({'key':'budget_max','value':str(int(amount)),'source':quote})
                continue
        if item.get('field') in ('city','district') and item.get('op') in ('eq','in') and item.get('priority')=='required':
            key='city' if item['field']=='city' else 'districts'
            patch.setdefault('fields',[]).append({'key':key,'value':json.dumps(item['values'],ensure_ascii=False),'source':quote})
            continue
        if item.get('field')=='is_new' and item.get('op') in ('eq','in') and item.get('priority')=='required':
            values=set(item['values'])
            if values and values<={'true','false'}:
                housing='both' if len(values)==2 else 'new' if 'true' in values else 'secondary'
                patch.setdefault('fields',[]).append({'key':'housing','value':housing,'source':quote})
                continue
        if item.get('field')=='rooms' and item.get('op') in ('eq','in') and item.get('priority')=='required':
            numbers=[float(v) for v in item['values']]
            if numbers and all(n.is_integer() and 0<=n<=5 for n in numbers):
                patch.setdefault('fields',[]).append({'key':'rooms_options','value':json.dumps([int(n) for n in numbers if n]),'source':quote})
                if 0 in numbers:patch['fields'].append({'key':'studio_requested','value':'true','source':quote})
                continue
        if item.get('field','').startswith('nearby:') and item.get('op')=='exists' and item.get('priority')!='remove':
            # «Рядом есть парк» использует объявленный продуктовый радиус,
            # а не существование парка на произвольном расстоянии.
            patch.setdefault('criteria',[]).append({'key':'poi:'+item['field'].split(':',1)[1],
                'priority':item['priority'],'value':'true','source':quote})
            continue
        patch.setdefault('fields',[]).append({'key':'filters','value':json.dumps([{k:v for k,v in item.items() if k!='source'}]),'source':quote})
    requested=patch.setdefault('requested_card',[])
    if requested is None:patch['requested_card']=[]
    elif isinstance(requested,str):patch['requested_card']=[requested]
    if patch.get('unsupported') is None:patch['unsupported']=[]
    if not isinstance(patch['unsupported'],list):raise ValueError('unsupported должен быть списком')
    texts=[r['text'].casefold() for r in inputs]
    aliases=aliases or {}
    def alias_for(key):
        def canonical(k):return k.replace('poi:','nearby:',1) if k.startswith('poi:') else k
        return next((k for k in aliases if isinstance(key,str) and canonical(k)==canonical(key)),None)
    def unsupported(key,quote,remove=False):
        if remove:
            if key not in aliases:raise ValueError('Для отмены неизвестного свойства используй прежнее имя из unsupported_conditions либо ключ из unsupported_properties')
            text=aliases[key]
        else:
            text=quote[:200]
            # Одно неподдержанное пожелание могло прийти явно и одновременно
            # как неверный фильтр. Сохраняем один текст и все его ключи-алиасы.
            matches={c['text'] for c in patch['unsupported'] if isinstance(c,dict)
                     and c.get('action')=='add' and c.get('text') and c['text'].casefold() in quote.casefold()}
            if len(matches)==1:text=matches.pop()
        patch['unsupported'].append({'text':text,'source':quote,'action':'remove' if remove else 'add','key':key})
    for group in ('fields','criteria'):
        rows=patch.get(group) or []
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise ValueError(group+': ожидается список объектов изменений')
        from .criteria import CRITERIA_KEYS
        allowed={*input_frame.basic_fields(CHANGE_SCHEMA),*CRITERIA_KEYS,'city','cities'}
        retained=[]
        for row in rows:
            if 'value' not in row and (row.get('action')=='remove' or row.get('priority')=='remove'):row['value']=''
            if 'value' in row and not isinstance(row['value'],str):row['value']=json.dumps(row['value'],ensure_ascii=False)
            quote=row.get('source')
            removing=(row.get('action')=='remove' or row.get('priority')=='remove'
                      or (row.get('value','').strip()=='false' and alias_for(row.get('key')) is not None))
            alias=alias_for(row.get('key')) if removing else None
            if alias and isinstance(quote,str):unsupported(alias,quote,True)
            if isinstance(row.get('key'),str) and row['key'] not in allowed and isinstance(quote,str) and quote and any(quote.casefold() in text for text in texts):
                if row['key'] in input_frame.FIELDS:
                    raise ValueError('Свойство '+row['key']+' поддерживается: запиши его через fields с key filters, оператором op и values, а не как неподдержанное условие')
                if not alias:unsupported(row['key'],quote,removing)
            else:retained.append(row)
        patch[group]=retained
    for row in patch['fields']:
        if row.get('key')!='filters':continue
        filters=json.loads(row['value'])
        if not isinstance(filters,list):continue
        retained=[]
        for f in filters:
            if (isinstance(f,dict) and alias_for(f.get('field')) and f.get('op')=='eq'
                    and f.get('values')==['false']):
                unsupported(alias_for(f['field']),row.get('source',''),True)
                continue
            alias=alias_for(f.get('field')) if isinstance(f,dict) and f.get('priority')=='remove' else None
            if alias:unsupported(alias,row.get('source',''),True)
            if isinstance(f,dict) and f.get('priority')=='remove':f['values']=[]
            if isinstance(f,dict) and isinstance(f.get('field'),str) and f['field'] not in input_frame.FIELDS:
                if not alias:unsupported(f['field'],row.get('source',''),f.get('priority')=='remove')
            elif unverifiable_distance(f):
                raise ValueError('Для '+f['field']+' требуется положительное расстояние в метрах; boolean и ноль не задают расстояние. Если речь об удобстве внутри квартиры, сохрани исходное пожелание как unsupported, не заменяй его объектом рядом.')
            else:retained.append(f)
        row['value']=json.dumps(retained,ensure_ascii=False)
    for condition in patch['unsupported']:
        if isinstance(condition,dict) and condition.get('action')=='remove' and condition.get('key') in aliases:
            condition['text']=aliases[condition['key']]
        elif isinstance(condition,dict) and condition.get('action')=='add':
            quote,label=condition.get('source'),condition.get('text')
            if (isinstance(quote,str) and quote.strip() and any(quote.casefold() in text for text in texts)
                    and isinstance(label,str) and label.strip() and not any(label.casefold() in text for text in texts)
                    and label not in aliases.values() and condition.get('key') not in aliases):
                # Класс unsupported модель указала явно. Для показа берём
                # исходную цитату, а не её пересказ: новых фактов это не добавляет.
                # Старое условие нельзя переносить на новую команду-альтернативу.
                condition['text']=quote[:200]
    validation.validate(patch,contract)
    known=set((profile or {}).get('_unsupported_conditions') or [])|set(aliases.values())
    for item in patch['withdraw_unsupported']:
        if item['text'] not in known:raise ValueError('Для снятия выберите сохранённое неподдержанное пожелание')
    frame={'fields':{},'criteria':{},'places':patch['places'],
           'unsupported':[*patch['unsupported'],*[{**item,'action':'remove'} for item in patch['withdraw_unsupported']]]}
    if patch['states']:
        modes={}
        for state in patch['states']:
            if not state['source'] or not any(state['source'].casefold() in text for text in texts):
                raise ValueError('Статус поля требует буквальной цитаты нового ввода')
            mode=mode_for(state['key'],state['state'],state['source'])
            if mode is None:continue
            if state['key'] in modes and modes[state['key']]!=mode:raise ValueError('Противоречащие статусы поля')
            modes[state['key']]=mode
        if modes:frame['fields']['field_states']={'value':modes,'source':patch['states'][0]['source']}
    known=input_frame.basic_fields(CHANGE_SCHEMA)
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
    explicit={row['key'] for row in patch['criteria']}
    for row in [*patch['fields'],*patch['criteria']]:
        key,priority=row['key'],row.get('priority','required')
        if row.get('action')=='remove' or row['value'].strip() in ('null',''):priority='remove'
        boolean=key.startswith(('poi:','feature:')) or key in ('not_first_floor','not_last_floor')
        if boolean and row['value'].strip()=='false':priority='remove'
        if key in ('city','cities'):
            from ..sources import normalize_city,SUPPORTED_CITIES
            raw=value(row['value'],{'type':'string'}).strip()
            names=json.loads(raw) if raw.startswith('[') else [raw]
            if not isinstance(names,list) or any(not isinstance(name,str) for name in names):
                raise ValueError('Название города должно быть строкой или списком строк')
            for name in names:
                city=normalize_city(name)
                if city not in SUPPORTED_CITIES:raise ValueError('Город нужно сопоставить с supported_cities; не подставляй похожий')
                words=[]
                for token in input_frame.quoted_words(row['source'],inputs):
                    lemmas=input_frame.place_lemmas(token,input_frame.catalog_words())
                    matches=[w for w in lemmas if w in city.split() or normalize_city(w)==city]
                    if len(matches)==1:words.append({'source':token,'lemma':matches[0]})
                if not words:
                    if profile and city in [profile.get('city'),*(profile.get('cities') or [])]:continue
                    raise ValueError('Название города требует исходных слов из новой реплики')
                frame['places'].append({'words':words})
            continue
        if key in known:
            if priority=='remove':
                kind=known[key]['type']
                if key in ('budget_max','housing','okrug'):
                    old=(frame['fields'].get('field_states') or {}).get('value',{})
                    mode=mode_for(key,'unrestricted',row['source'])
                    if mode is None:continue
                    frame['fields']['field_states']={'value':{**old,key:mode},'source':row['source']}
                    continue
                if kind not in ('boolean','array'):raise ValueError('Для изменения '+key+' нужно новое значение')
                actual=False if kind=='boolean' else []
            else:actual=value(row['value'],known[key])
            if key in ('budget_max','matcap') and type(actual) in (int,float):
                from .numbers import ground_money
                actual=ground_money(actual,row['source'],inputs)
            put('fields',key,{'value':actual,'source':row['source']})
            continue
        if 'priority' not in row and priority!='remove' and key in explicit:continue
        if key.startswith(('poi:','feature:')) or key in ('not_first_floor','not_last_floor'):
            put('criteria',key,priority)
        else:
            put('criteria',key,{'value':None if priority=='remove' else value(row['value'],input_frame.criterion_value(key,CHANGE_SCHEMA)),
                               'priority':priority})
    if patch.get('selection'):
        q=deepcopy(patch['selection'][0]);q['limit']=q['limit'] or None;q['group_by']=q['group_by'] or None
        frame['selection']=q
    if patch['requested_card']:frame['_requested_action']=patch['requested_card'][0]
    from .conditions import property_id
    quotes={property_id(row['key']):{'kind':'user','quote':row['source']}
            for group in ('fields','criteria') for row in patch[group]}
    for row in patch['fields']:
        if row['key']=='filters':
            for f in (frame['fields'].get('filters') or {}).get('value',[]):
                quotes[f['field']]={'kind':'user','quote':row['source']}
    if provenance is not None:provenance.update(quotes)
    return frame
