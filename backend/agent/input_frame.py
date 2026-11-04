"""Типизированный ввод: модель понимает речь, код связывает условие с его приоритетом."""
from copy import deepcopy
from difflib import SequenceMatcher
import re

from . import validation
from .criteria import CRITERIA_KEYS, SCALARS
from .features import FEATURES
from .. import geo
from .listing_query import FIELDS,validate_filters
from . import selection_request

EXCLUDED = {'city','cities','rooms','features','poi','soft_criteria','clear_criteria', *SCALARS}
INTENTS = ['search','refine','selection','call','seller_questions','question','continue','hold','other']


def basic_fields(changes):
    fields={k:deepcopy(v) for k,v in changes['properties'].items() if k not in EXCLUDED}
    fields['rooms_options']['items']['minimum']=1
    fields['rooms_options']['description']='Все явно допустимые числа комнат 1–5. 1–2 комнаты -> [1,2], без студии. Неизвестность — null. Студия задаётся отдельно studio_requested.'
    fields['studio_requested']={'type':'boolean','description':'true — клиент явно просит/допускает студию; false — явно исключает. Неназванная студия — null, не true.'}
    return fields


def criterion_value(key, changes):
    if key.startswith(('poi:', 'feature:')):
        return {'type':'boolean','enum':[True]}
    return deepcopy(changes['properties'][key])


def object_schema(properties):
    return {'type':'object','additionalProperties':False,'required':list(properties),'properties':properties}


def nullable(value):
    value=deepcopy(value)
    value['type']=[value['type'],'null']
    if 'enum' in value:value['enum'].append(None)
    return value


def source_words(messages):
    """Словарь буквальных ссылок для схемы, без извлечения смысла или параметров."""
    punctuation='.,!?…:;()[]{}«»"'
    return list(dict.fromkeys(word.strip(punctuation) for message in messages
        for word in message['text'].split() if word.strip(punctuation)))


def place_lemmas(source,words):
    """Привязка сущности к словарю, не разбор команды/намерения пользователя."""
    raw=source.casefold().replace('ё','е')
    scores={word:SequenceMatcher(None,raw,word.casefold().replace('ё','е')).ratio()
            for word in words if len(raw)>=2 and raw[:3]==word.casefold().replace('ё','е')[:3]}
    best=max(scores.values(),default=0)
    return [word for word,score in scores.items() if score==best and score>=.6]


def catalog_words():
    from ..sources import SUPPORTED_CITIES
    return sorted({word for city in SUPPORTED_CITIES for word in city.split()} |
                  {'Подмосковье','Питер','СПб','МСК','МО'})


def schema(changes, messages=None):
    place_words = catalog_words()
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
    source={'type':'string','maxLength':80,'description':'Выбери одно исходное слово из enum, сохранив его форму и регистр.'}
    if messages is not None:
        source['enum']=[word for word in source_words(messages) if place_lemmas(word,place_words)]
    field_source = {'type':'string','maxLength':1000,'description':'Буквальное слово нового ввода, подтверждающее значение; либо всё исходное сообщение. Не цитата из профиля.'}
    if messages is not None:
        field_source['enum'] = list(dict.fromkeys([*source_words(messages), *(row['text'] for row in messages)]))
    return object_schema({
        'places':{'type':'array','maxItems':0 if messages is not None and not source['enum'] else 3,'description':'Все явно названные допустимые города/области, включая варианты через «или». Москва и Подмосковье — два элемента. При неизменном городе пусто. Центр, школа, новостройка, срок сдачи НЕ территории.',
            'items':object_schema({'words':{'type':'array','minItems':1,'maxItems':8,'items':object_schema({
                'source':source,
                'lemma':{'type':'string','enum':place_words,'description':'Слово названия города в начальной форме. Не добавляй отсутствующие слова. Неподдержанный город — предметный question и places=[]; не подставляй похожий.'}})}})},
        'fields':nullable(object_schema({key:nullable(object_schema({'value':value,'source':field_source}))
                               for key,value in basic_fields(changes).items()})),
        'criteria':nullable(object_schema(criteria)),
        'selection':nullable(selection_request.SCHEMA),
        'unsupported':{'type':'array','maxItems':5,'description':'Явно запрошенные условия, которых нет среди проверяемых полей/критериев. Не объявляй такие квартиры несуществующими.',
            'items':object_schema({'text':{'type':'string','maxLength':200},'source':field_source,
                                  'action':{'type':'string','enum':['add','remove']}})},
        'call_ids':nullable({'type':'array','maxItems':10,'items':{'type':'string','maxLength':80},
                            'description':'Только при явной просьбе ЗВОНИТЬ: ID из показанных карточек; [] — по понравившимся. Иначе null. Вопросы продавцу не разрешают звонок.'}),
        'question':{'type':'string','maxLength':500,'description':'Один вопрос только при неясности, иначе пустая строка.'},
        'options':{'type':'array','maxItems':5,'items':{'type':'string','maxLength':100}},
        'intent':{'type':'string','enum':INTENTS,'description':'selection — обработать текущие результаты, сравнить, оставить N; call — явно позвонить; seller_questions — добавить вопросы продавцу; refine — фильтры; question — информация; search — поиск; continue — продолжить; hold — стоп.'},
    })


def sparse_schema(changes,messages):
    value=schema(changes,messages)
    value['required']=['intent']
    value['properties']['fields']['required']=[]
    value['properties']['criteria']['required']=[]
    value['properties']['selection']['required']=['operation']
    if not value['properties']['places']['items']['properties']['words']['items']['properties']['source']['enum']:
        # Не передаём недопустимый enum=[]; maxItems=0 уже запрещает территории.
        value['properties']['places']['items']['properties']['words']['items']['properties']['source'].pop('enum')
    return value


def complete_frame(result,changes_schema):
    if not isinstance(result,dict):raise ValueError('Ожидался объект смысла реплики')
    value={'fields':None,'criteria':None,'places':[],'selection':None,'call_ids':None,'unsupported':[],
           'question':'','options':[],**deepcopy(result)}
    if value['fields'] is not None:
        value['fields']={**{key:None for key in basic_fields(changes_schema)},**value['fields']}
    if value['criteria'] is not None:value['criteria']={**{key:None for key in CRITERIA_KEYS},**value['criteria']}
    if isinstance(value['selection'],dict):
        op=value['selection'].get('operation')
        value['selection']={'scope':'shown' if op=='compare' else 'all','limit':None,'sort':None,'aggregates':None,'group_by':None,**value['selection']}
    return value


def decode(frame, profile, payload, changes_schema):
    """Единственный внешний формат — nullable fields/criteria по схеме модели."""
    validation.validate(frame, schema(changes_schema))
    frame=deepcopy(frame)
    frame['fields']=frame['fields'] or {}
    frame['criteria']=frame['criteria'] or {}
    intent = frame['intent']
    query,operation=(selection_request.compile_request(frame['selection'],payload.get('workflow',{}).get('selection_query'))
                     if frame['selection'] is not None else (None,'rank'))
    commands = {'selection':query,'selection_operation':operation,'call_ids':frame['call_ids'],'unsupported':frame['unsupported']}
    input_texts = [row['text'].casefold() for row in payload['messages']]
    for condition in frame['unsupported']:
        if not condition['source'].strip() or not any(condition['source'].casefold() in text for text in input_texts):
            raise ValueError('Неподдержанное пожелание требует цитаты нового ввода')
        if condition['action']=='add' and (not condition['text'].strip() or not any(condition['text'].casefold() in text for text in input_texts)):
            raise ValueError('unsupported.add.text — буквальная цитата НОВОГО ввода. Не переноси старое неподдержанное условие в ответ на выбор альтернативы; тогда unsupported=[]')
    if operation in ('compare','analyze','more') and not any(x['action']=='add' for x in frame['unsupported']):
        # Чтение найденных данных не имеет полномочий изменять профиль.
        frame=deepcopy(frame)
        frame['fields']={k:None for k in frame['fields']}
        frame['criteria']={k:None for k in frame['criteria']}
        frame['places']=[]
        intent='selection'
    if any(x['action']=='add' for x in frame['unsupported']) and intent not in ('search','refine','hold'):
        raise ValueError('Неподдержанное новое условие — refine/search/hold, не обычный вопрос и не выполненная команда')
    if intent not in ('search', 'refine', 'selection', 'call', 'seller_questions', 'hold'):
        if frame['selection'] is not None or frame['call_ids'] is not None:
            raise ValueError('Информационный вопрос/остановка не могут исполнять команды')
        return {'intent': intent, 'decision': 'unchanged', 'changes': {}, 'question': '', 'options': []}
    if intent == 'hold' and (frame['selection'] is not None or frame['call_ids'] is not None):
        raise ValueError('Остановка не разрешает поиск или звонок')
    if intent=='refine' and not (any(v is not None for v in frame['fields'].values()) or
            any(v is not None for v in frame['criteria'].values()) or frame['places'] or
            frame['selection'] or frame['unsupported'] or frame['question'].strip()):
        raise ValueError('Пустое уточнение: укажи названное изменение или конкретный вопрос о неясности. Если изменений нет, выбери continue/other/question.')
    changes = {}
    for key,slot in frame['fields'].items():
        if slot is None:
            continue
        value,quote = slot['value'],slot['source'].strip()
        if value == profile.get(key):
            continue
        if not quote or not any(quote.casefold() in text for text in input_texts):
            # Пустые неназванные значения от модели не стирают прежние условия.
            if value in (False, [], ''):
                continue
            raise ValueError(f'Изменение {key} требует точной цитаты нового ввода; неназванное поле должно быть null')
        changes[key] = value
    studio=changes.pop('studio_requested',None)
    if studio is not None:
        rooms=list(changes.get('rooms_options',profile.get('rooms_options') or []))
        rooms=sorted(set([*rooms,0] if studio else [n for n in rooms if n!=0]))
        if rooms!=profile.get('rooms_options',[]):changes['rooms_options']=rooms
        else:changes.pop('rooms_options',None)
    if intent == 'selection' and set(changes)=={'filters'}:
        # Составной запрос «сдача в 2027, покажи три больших» содержит и
        # условие, и команду выдачи. Тег нормализуется по типу данных, без
        # повторного языкового разбора и без допуска к изменению комнат/семьи.
        intent = 'refine'
    if intent == 'selection' and changes:
        raise ValueError('selection меняет выдачу, не профиль. Неназванные поля '+', '.join(changes)+' должны быть null; количество — selection.limit. Явные новые условия требуют refine')
    if 'filters' in changes:
        validate_filters(changes['filters'])
        # Проверка буквальных чисел — не классификация намерения. Модель
        # не вправе добавлять 2026 год или радиус 3 км к запросу с бюджетом
        # 30 млн. Словесные числа остаются задачей смыслового разбора.
        literals=[float(n.replace(' ','').replace('\u00a0','').replace(',','.'))
                  for text in input_texts for n in re.findall(r'(?<!\w)\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?',text)]
        if literals:
            supported={n*scale for n in literals for scale in (1,1000,1_000_000,1_000_000_000)}
            for item in changes['filters']:
                if item['priority']=='remove':
                    continue
                if FIELDS[item['field']][0]!='number':continue
                for value in item['values']:
                    if float(value) not in supported:
                        raise ValueError('Неназванный числовой фильтр '+item['field']+'='+value+': такого числа нет в новом сообщении. Убери предположительный фильтр.')
    if frame['call_ids'] is not None and intent != 'call':
        raise ValueError('Звонок требует отдельного явного намерения клиента')
    soft = set(profile.get('soft_criteria') or [])
    lists = {key: list(profile.get(key) or []) for key in ('poi', 'features')}
    for key, value in frame['criteria'].items():
        if value is None:
            continue
        if isinstance(value, str):
            priority, actual = value, True
        else:
            priority, actual = value['priority'], value['value']
        if priority == 'unmentioned' or (actual is None and priority != 'remove'):
            continue
        removing = priority == 'remove'
        if removing and isinstance(value, dict) and actual is not None:
            raise ValueError('Снятие условия требует value=null')
        soft.discard(key)
        if removing:
            changes.setdefault('clear_criteria', []).append(key)
        elif priority == 'preferred':
            soft.add(key)
        if key.startswith(('poi:', 'feature:')):
            group, name = ('poi' if key.startswith('poi:') else 'features'), key.split(':', 1)[1]
            lists[group] = [item for item in lists[group] if item != name]
            if not removing:
                lists[group].append(name)
            changes[group] = lists[group]
        elif not removing:
            changes[key] = actual
        changes['soft_criteria'] = sorted(soft)
    canonical = {city.casefold(): city for city in payload['supported_cities']}
    word_sets = {tuple(sorted(city.casefold().split())): city for city in payload['supported_cities']}
    ambiguous = {key.casefold(): value for key, value in payload['ambiguous_catalog_names'].items()}
    choices = [item for item in payload['messages'][-1].get('options', []) if item in payload['supported_cities']]
    source_tokens = {word.casefold() for word in source_words(payload['messages'])}
    places = []
    lemma_words=catalog_words()
    question, options = frame['question'], frame['options']
    for row in frame['places']:
        seen = set()
        valid = True
        for word in row['words']:
            raw, lemma = word['source'].strip().casefold(), word['lemma'].strip()
            if raw not in source_tokens or raw in seen or not lemma or any(char.isspace() for char in lemma):
                valid = False
            from ..sources import normalize_city
            if lemma not in place_lemmas(word['source'],lemma_words):
                if not (normalize_city(word['source']) and normalize_city(word['source'])==normalize_city(lemma)):
                    valid=False
            seen.add(raw)
        name = ' '.join(word['lemma'].strip() for word in row['words'])
        normalized = name.casefold()
        matched = canonical.get(normalized) or word_sets.get(tuple(sorted(normalized.split())))
        if not matched and choices:
            words = set(normalized.split())
            candidates = [city for city in choices if words <= set(city.casefold().split())]
            if len(candidates) == 1:
                matched = candidates[0]
        if not valid:
            if matched and matched in {profile.get('city'), *(profile.get('cities') or [])}:
                continue
            raise ValueError('Каждому исходному слову названия нужна одна начальная форма, без добавления слов')
        if matched:
            places.append(matched)
        elif normalized in ambiguous:
            options = ambiguous[normalized]
            question = 'Какой город вы имеете в виду: ' + ' или '.join(options) + '?'
        else:
            # Справочник синонимов выделенного моделью топонима, не разбор запроса.
            from ..sources import normalize_city
            alias = normalize_city(name)
            if alias in payload['supported_cities']:
                places.append(alias)
            else:
                candidates=[city for city in payload['supported_cities'] if set(normalized.split())<=set(city.casefold().split())]
                options=candidates[:5]
                question=('Вы имеете в виду '+candidates[0]+'?' if len(candidates)==1 else
                          'Какой город вы имеете в виду: '+ ' или '.join(options)+'?' if options else
                          'Не смог сопоставить название «'+name+'» с городом. Напишите полное название города.')
    if places:
        places = list(dict.fromkeys(places))
        changes.update(city=places[0], cities=places[1:])
    if intent == 'selection' and changes:
        raise ValueError('selection меняет только выдачу, не условия профиля. Для нового условия и запроса к выдаче вместе используй intent=refine и selection')
    if question.strip():
        return {'intent': intent, 'decision': 'clarify', 'changes': {}, 'question': question, 'options': options,
                'selection':None,'call_ids':None,'unsupported':[]}
    if options:
        raise ValueError('Варианты ответа требуют уточняющего вопроса')
    return {'intent': intent, 'decision': 'apply' if changes else 'unchanged', 'changes': changes,
            'question': '', 'options': [], **commands}
