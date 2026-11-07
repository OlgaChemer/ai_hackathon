"""Запрос исследования: тот же исполнитель полей и агрегатов, что у подборки."""
from copy import deepcopy
from . import listing_query as Q,validation

SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'respect_budget':{'type':'boolean','description':'«На сколько комнат/какую площадь хватит бюджета» — true: исследуем доступное в пределах суммы. Средняя цена рынка без ограничения бюджета — false.'},
    'group_by':{**deepcopy(Q.QUERY_SCHEMA['properties']['group_by']), 'description':'Исследуемое свойство: «на сколько комнат хватит» -> rooms. Если нужна одна общая оценка площади/цены — null, а метрика задаётся aggregates.'},
    'aggregates':deepcopy(Q.QUERY_SCHEMA['properties']['aggregates'])},
    'required':['respect_budget','group_by','aggregates']}


def query(value=None):
    value=value or {'respect_budget':False,'group_by':None,'aggregates':[]}
    validation.validate(value,SCHEMA)
    q={**Q.default_query(),'limit':30,'group_by':value['group_by'],
       'aggregates':value['aggregates'] or [{'field':'price','function':'median'},{'field':'price','function':'min'}]}
    Q.validate_query(q)
    return {**value,'aggregates':q['aggregates']},q


def execute(rows,value=None):
    _,q=query(value)
    result=Q.execute(rows,q)
    return {k:result[k] for k in ('total','groups','aggregates')}


def profile(s):
    """Снимок для источника; задача исследования не изменяет пожелания покупателя."""
    return {**s.profile,'_research_query':s.research_query}
