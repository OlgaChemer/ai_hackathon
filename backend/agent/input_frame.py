"""Типизированный ввод: модель понимает речь, код связывает условие с его приоритетом."""
from copy import deepcopy

from . import validation
from .criteria import CRITERIA_KEYS, SCALARS
from .features import FEATURES
from .. import geo

EXCLUDED = {'city','cities','features','poi','soft_criteria','clear_criteria', *SCALARS}
INTENTS = ['search','refine','question','continue','hold','other']


def basic_fields(changes):
    fields={k:deepcopy(v) for k,v in changes['properties'].items() if k not in EXCLUDED}
    fields['rooms_options']['description']='Все допустимые числа жилых комнат. 0 — только студия, 1 — однокомнатная, 2 — двухкомнатная и т.д. Неизвестность — null, не 0. Не сужай диапазон или перечисление.'
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


def schema(changes, messages=None):
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
        source['enum']=source_words(messages)
    return object_schema({
        'places':{'type':'array','maxItems':3,'description':'Только названные итоговые территории поиска. Если не менялись — пустой список.',
            'items':object_schema({'words':{'type':'array','minItems':1,'maxItems':8,'items':object_schema({
                'source':source,
                'lemma':{'type':'string','maxLength':80,'description':'Начальная форма этого слова. Не добавляй отсутствующие слова.'}})}})},
        'fields':object_schema({key:nullable(value) for key,value in basic_fields(changes).items()}),
        'criteria':object_schema(criteria),
        'question':{'type':'string','maxLength':500,'description':'Один вопрос только при неясности, иначе пустая строка.'},
        'options':{'type':'array','maxItems':5,'items':{'type':'string','maxLength':100}},
        'intent':{'type':'string','enum':INTENTS,'description':'question — информационный вопрос; refine — изменить условия; hold — пока не искать; search — начать; continue — продолжить; other — остальное.'},
    })


def decode(frame, profile, payload, changes_schema):
    """Единственный внешний формат — nullable fields/criteria по схеме модели."""
    validation.validate(frame, schema(changes_schema))
    intent = frame['intent']
    if intent not in ('search', 'refine'):
        return {'intent': intent, 'decision': 'unchanged', 'changes': {}, 'question': '', 'options': []}
    changes = {key: value for key, value in frame['fields'].items() if value is not None}
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
    question, options = frame['question'], frame['options']
    for row in frame['places']:
        seen = set()
        valid = True
        for word in row['words']:
            raw, lemma = word['source'].strip().casefold(), word['lemma'].strip()
            if raw not in source_tokens or raw in seen or not lemma or any(char.isspace() for char in lemma):
                valid = False
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
                question = f'Города «{name}» пока нет среди поддерживаемых. В каком городе искать?'
                options = []
    if places:
        places = list(dict.fromkeys(places))
        changes.update(city=places[0], cities=places[1:])
    if question.strip():
        return {'intent': intent, 'decision': 'clarify', 'changes': {}, 'question': question, 'options': options}
    if options:
        raise ValueError('Варианты ответа требуют уточняющего вопроса')
    return {'intent': intent, 'decision': 'apply' if changes else 'unchanged', 'changes': changes,
            'question': '', 'options': []}
