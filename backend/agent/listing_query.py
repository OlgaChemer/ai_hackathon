"""Ограниченный язык запросов к объявлениям: данные, сравнения, порядок и агрегаты.

Здесь нет eval/SQL и распознавания клиентского текста. Модель комбинирует
известные поля и операции; один исполнитель используется фильтрами и подборкой.
"""
from __future__ import annotations

import math
import re
import statistics
from copy import deepcopy

# Тип и подпись — единый каталог для схемы модели, исполнения и объяснений.
FIELDS = {
    'price': ('number', 'цена, ₽'), 'area': ('number', 'общая площадь, м²'),
    'kitchen': ('number', 'кухня, м²'), 'rooms': ('number', 'число комнат'),
    'floor': ('number', 'этаж'), 'floors': ('number', 'этажей в доме'),
    'year': ('number', 'год постройки'),
    'delivery_year': ('number', 'год сдачи новостройки'),
    'delivery_quarter': ('number', 'квартал сдачи новостройки'),
    'center_km': ('number', 'км по прямой до опорной точки центра'),
    'price_per_m2': ('number', 'цена за м²'),
    'value_score': ('number', 'полезность на млн ₽'),
    'discount_pct': ('number', 'отклонение вниз от оценки по аналогам, %'),
    'city': ('string', 'город'), 'district': ('string', 'район'),
    'condition': ('string', 'ремонт'), 'material': ('string', 'материал дома'),
    'is_new': ('boolean', 'новостройка'), 'finished': ('boolean', 'дом сдан'),
    'metro_minutes': ('number', 'минут пешком до метро'),
}
# Поздний импорт geo избегает зависимости от модели/действий.
POI_KINDS = ('school','kindergarten','clinic','hospital','park','water','pharmacy','shop','mall','fitness')
for _kind in POI_KINDS:
    FIELDS[f'nearby:{_kind}'] = ('number', f'расстояние по прямой до {_kind}, м')

FILTER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['field','op','values','priority'],
    'properties': {
        'field': {'type':'string','enum':list(FIELDS)},
        'op': {'type':'string','enum':['eq','ne','lt','lte','gt','gte','in','between','exists']},
        'values': {'type':'array','maxItems':20,'items':{'type':'string','maxLength':120},
                   'description':'Значения в типе поля: число строкой (2027), true/false, текст. Для between два значения; exists — пустой список.'},
        'priority': {'type':'string','enum':['required','preferred','remove']},
    },
}
PROFILE_FILTER_SCHEMA = deepcopy(FILTER_SCHEMA)
PROFILE_FILTER_SCHEMA['properties']['field']['enum'] = [k for k in FIELDS if k not in ('value_score','discount_pct')]
QUERY_SCHEMA = {
    'type':'object','additionalProperties':False,
    'required':['scope','mode','limit','filters','sort','aggregates','group_by'],
    'properties': {
        'scope': {'type':'string','enum':['all','shown','liked']},
        'mode': {'type':'string','enum':['rank','more']},
        'limit': {'type':'integer','minimum':1,'maximum':30},
        'group_by': {'type':['string','null'],'enum':[*FIELDS,None]},
        'filters': {'type':'array','maxItems':20,'items':FILTER_SCHEMA},
        'sort': {'type':'array','maxItems':4,'items':{
            'type':'object','additionalProperties':False,'required':['field','direction'],
            'properties':{'field':{'type':'string','enum':list(FIELDS)},'direction':{'type':'string','enum':['asc','desc']}}}},
        'aggregates': {'type':'array','maxItems':6,'items':{
            'type':'object','additionalProperties':False,'required':['field','function'],
            'properties':{'field':{'type':'string','enum':list(FIELDS)},'function':{'type':'string','enum':['count','min','max','mean','median']}}}},
    },
}


def default_query():
    return {'scope':'all','mode':'rank','limit':10,'filters':[],'sort':[],'aggregates':[],'group_by':None}


def field_value(x, field):
    if field == 'center_km':
        from .pricing import center_distance
        return center_distance(x)
    if field == 'price_per_m2':
        return x['price'] / x['area'] if x.get('area') and x.get('price') else None
    if field in ('value_score','discount_pct'):
        return (x.get('ranking') or {}).get(field)
    if field.startswith('nearby:'):
        kind = field.split(':',1)[1]
        nearby=x.get('nearby') or {}
        item = next((p for p in nearby if p.get('kind')==kind),None) if isinstance(nearby,list) else nearby.get(kind)
        return item.get('dist') if item else None
    if field == 'metro_minutes':
        metro = x.get('metro') or {}
        return metro.get('minutes') if metro.get('mode') == 'пешком' else None
    if field in ('delivery_year','delivery_quarter'):
        if x.get(field) is not None:
            return x[field]
        # Только формат данных источника/старого кэша, не свободный ввод пользователя.
        deadline = x.get('deadline') or ''
        if field == 'delivery_year':
            found = re.search(r'\b(20\d{2})\b', deadline)
        else:
            found = re.search(r'\b([1-4])\s*кв', deadline)
        return int(found[1]) if found else None
    value = x.get(field)
    if value is None:
        return None
    kind = FIELDS[field][0]
    if kind == 'number':
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (ValueError,TypeError):
            return None
    if kind == 'boolean':
        return value if type(value) is bool else None
    return str(value)


def _typed(value, field):
    kind = FIELDS[field][0]
    if kind == 'number':
        try:
            result = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Числовое поле {field}: values допускает только числа строкой, без имён полей и слов. Используй подходящий базовый criteria вместо неподдержанного сравнения') from exc
        if not math.isfinite(result):
            raise ValueError('Нечисловое значение фильтра')
        return result
    if kind == 'boolean':
        if value not in ('true','false'):
            raise ValueError('Логическое значение должно быть true/false')
        return value == 'true'
    return str(value).casefold()


def validate_filters(filters):
    from .validation import validate
    for f in filters:
        validate(f, FILTER_SCHEMA)
        values = [_typed(v, f['field']) for v in f['values']]
        size = len(values)
        if f['priority'] == 'remove':
            continue
        op = f['op']
        if ((op == 'exists' and size != 0) or (op == 'between' and size != 2)
                or (op == 'in' and size < 1) or (op not in ('exists','between','in') and size != 1)):
            expected = '0' if op == 'exists' else '2' if op == 'between' else '1 или больше' if op == 'in' else '1'
            raise ValueError(f"Фильтр {f['field']}: {op} требует values длины {expected}, получено {size}. Укажи значения из нового ввода")
        if op in ('lt','lte','gt','gte','between') and FIELDS[f['field']][0] != 'number':
            raise ValueError('Сравнение диапазона требует числового поля')
        if op == 'between' and values[0] > values[1]:
            raise ValueError('Перепутаны границы диапазона')


def validate_query(query):
    from .validation import validate
    validate(query, QUERY_SCHEMA)
    validate_filters(query['filters'])
    for a in query['aggregates']:
        if a['function'] != 'count' and FIELDS[a['field']][0] != 'number':
            raise ValueError('Расчёт требует числового поля')


def check(x, f):
    actual = field_value(x, f['field'])
    op = f['op']
    if op == 'exists':
        return actual is not None
    if actual is None:
        return None
    try:
        actual = _typed(str(actual).lower() if isinstance(actual,bool) else actual, f['field'])
    except (ValueError,TypeError):
        return None
    values = [_typed(v, f['field']) for v in f['values']]
    if op == 'in': return actual in values
    if op == 'between': return values[0] <= actual <= values[1]
    v = values[0]
    return {'eq':lambda:actual == v,'ne':lambda:actual != v,'lt':lambda:actual < v,
            'lte':lambda:actual <= v,'gt':lambda:actual > v,'gte':lambda:actual >= v}[op]()


def describe(f):
    signs = {'eq':'=','ne':'≠','lt':'<','lte':'≤','gt':'>','gte':'≥','in':'из','between':'диапазон','exists':'указано'}
    return f"{FIELDS[f['field']][1]} {signs[f['op']]} {', '.join(f['values'])}".strip()


def filter_checks(x, filters):
    rows = []
    for f in filters:
        if f['priority'] == 'remove': continue
        actual = field_value(x,f['field'])
        rows.append({'key':'filter:'+f['field'],'label':describe(f),'status':check(x,f),
                     'required':f['priority']=='required','source':'map' if f['field']=='center_km' or f['field'].startswith('nearby:') else 'cian',
                     'evidence':str(actual) if actual is not None else None,'checked_at':None})
    return rows


def execute(items, query, *, liked=(), shown=(), seen=()):
    """Чистое выполнение запроса. Не меняет профиль, входной пул или согласия."""
    validate_query(query)
    scope = set(liked if query['scope']=='liked' else shown)
    rows = [x for x in items if query['scope']=='all' or x['id'] in scope]
    rows = [x for x in rows if all(check(x,f) is True for f in query['filters'] if f['priority']=='required')]
    if query['mode'] == 'more':
        rows = [x for x in rows if x['id'] not in set(seen)]
    preferred=[f for f in query['filters'] if f['priority']=='preferred']
    if preferred:
        rows=sorted(rows,key=lambda x:sum(6 if check(x,f) is True else -4 if check(x,f) is False else 0 for f in preferred),reverse=True)
    for sort in reversed(query['sort']):
        known = [x for x in rows if field_value(x,sort['field']) is not None]
        missing = [x for x in rows if field_value(x,sort['field']) is None]
        rows = sorted(known,key=lambda x:field_value(x,sort['field']),reverse=sort['direction']=='desc') + missing
    aggregates=[]
    for a in query['aggregates']:
        values=[field_value(x,a['field']) for x in rows if field_value(x,a['field']) is not None]
        fn=a['function']
        value = len(values) if fn=='count' else ({'min':min,'max':max,'mean':statistics.mean,'median':statistics.median}[fn](values) if values else None)
        aggregates.append({**a,'value':value,'known':len(values),'unknown':len(rows)-len(values)})
    groups=[]
    if query['group_by']:
        grouped={}
        for x in rows:
            grouped.setdefault(field_value(x,query['group_by']),[]).append(x)
        for key,items in sorted(grouped.items(),key=lambda item:(item[0] is None,str(item[0]))):
            sub=execute(items,{**query,'group_by':None,'scope':'all','mode':'rank'})
            groups.append({'value':key,'count':len(items),'aggregates':sub['aggregates']})
    return {'items':rows[:query['limit']],'total':len(rows),'remaining':max(0,len(rows)-query['limit']),
            'aggregates':aggregates,'groups':groups}
