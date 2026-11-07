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
                     'description':'rank — команда отобрать/пересортировать карточки; compare — вопрос о победителе: какая самая просторная, у какой дешевле метр. Сохраняет карточки; more — новые варианты; analyze — расчёт без изменения карточек'},
        'scope':{**deepcopy(Q.QUERY_SCHEMA['properties']['scope']),
                 'description':'liked — только понравившиеся/избранные, даже если отметок пока нет; shown — эти/показанные/из них (ссылка на текущие карточки); all — весь найденный пул. Не расширяй заданную область.'},
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
           'limit':request['limit'] if request['limit'] is not None else previous.get('limit',Q.default_query()['limit']),
           'sort':deepcopy(request['sort'] if request['sort'] is not None else previous.get('sort',[])),
           'aggregates':deepcopy(request['aggregates'] or []),'group_by':request['group_by']}
    if operation=='more': query['scope']='all'
    if operation=='compare' and not query['sort']:
        raise ValueError('Для сравнения нужен показатель: цена за м², площадь, цена или оценка')
    Q.validate_query(query)
    return query,operation


def set_visible_scope(s,marks):
    """Привязать отсылку «эти квартиры» к странице того же снимка подборки."""
    ids=(marks or {}).get('visible_listing_ids')
    known={x['id'] for x in s.shown}
    if ids is None:
        s.selection_scope_ids=[x['id'] for x in s.shown]
        return True
    if ((marks or {}).get('selection_revision')!=s.selection_revision
            or not isinstance(ids,list) or len(ids)>30 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids)
            or any(i not in known for i in ids) or (known and not ids)):
        return False
    s.selection_scope_ids=list(ids)
    return True
