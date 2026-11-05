"""Модель описывает цель чтения; исполняемый запрос собирает код.

В сортировке/сравнении нет числовых предикатов: «из двух» не может стать
порогом цены 2 ₽. Явные новые условия идут через отдельный профиль запроса.
"""
from copy import deepcopy

from . import listing_query as Q
from .validation import validate

SCHEMA = {
    'type':'object','additionalProperties':False,
    'required':['operation','scope','limit','sort','aggregates','group_by'],
    'properties':{
        'operation':{'type':'string','enum':['rank','compare','more','analyze'],
                     'description':'rank — отобрать/пересортировать; compare — ответить, какая из квартир лучше по показателю, сохранив карточки; more — новые варианты; analyze — расчёт без изменения карточек'},
        'scope':{**deepcopy(Q.QUERY_SCHEMA['properties']['scope']),
                 'description':'liked — только понравившиеся/избранные, даже если отметок пока нет; shown — эти/показанные; all — весь найденный пул. Не расширяй явно заданную область.'},
        'limit':{'type':['integer','null'],'minimum':1,'maximum':30,
                 'description':'Количество предложений. null — сохранить размер выдачи. Это не комнатность и не порог цены.'},
        'sort':{**deepcopy(Q.QUERY_SCHEMA['properties']['sort']),'type':['array','null'],
                'description':'null — сохранить прежний порядок; [] — обычный рейтинг. Цена за м² выгоднее: price_per_m2 asc.'},
        'aggregates':{**deepcopy(Q.QUERY_SCHEMA['properties']['aggregates']),'type':['array','null']},
        'group_by':deepcopy(Q.QUERY_SCHEMA['properties']['group_by']),
    },
}


def compile_request(request, previous=None):
    validate(request,SCHEMA)
    previous=previous or Q.default_query()
    operation=request['operation']
    query={**Q.default_query(), 'scope':request['scope'],
           'mode':'more' if operation=='more' else 'rank',
           'limit':request['limit'] if request['limit'] is not None else previous.get('limit',10),
           'sort':deepcopy(request['sort'] if request['sort'] is not None else previous.get('sort',[])),
           'aggregates':deepcopy(request['aggregates'] or []),'group_by':request['group_by']}
    if operation=='more': query['scope']='all'
    if operation=='compare' and not query['sort']:
        raise ValueError('Для сравнения нужен показатель: цена за м², площадь, цена или оценка')
    Q.validate_query(query)
    return query,operation
