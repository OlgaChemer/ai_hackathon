"""Помочь сформулировать запрос перед первым подбором, без обязательных чисел."""
from copy import deepcopy

FIELDS=('budget_max','rooms')


def scope(s,field):
    return {'city':s.profile.get('city'),'cities':s.profile.get('cities') or [],
            'budget':s.profile.get('budget_max') if field=='rooms' else None}


def discussed(s,field):
    return (s.guidance.get('assisted') or {}).get(field)==scope(s,field)


def next_step(s):
    if not s.profile.get('city') or s.search is not None:return None
    if s.shown and not s.selection_profile_stale:return None
    follow=s.guidance.get('followup') or {}
    if follow.get('seq')==s.request_review_seq:
        return 'followup',follow['field']
    states=s.profile.get('field_states') or {}
    for field in FIELDS:
        if s.profile.get(field) is not None or states.get(field)=='unrestricted' or discussed(s,field):continue
        return ('help' if states.get(field)=='unknown' else 'ask'),field
    return None


def help_field(s):
    step=next_step(s)
    return step[1] if step and step[0]=='help' else None


def search_ready(s):
    return next_step(s) is None


def research_query(s):
    return {'respect_budget':s.profile.get('budget_max') is not None,'group_by':'rooms',
            'aggregates':[{'field':'price','function':'min'},{'field':'price','function':'median'}]}


def record_market(s,success=True):
    """Попытка помощи и следующий вопрос сохраняются вместе с результатом инструмента."""
    target=help_field(s)
    addressed=[]
    if success:
        if s.profile.get('budget_max') is None:addressed.append('budget_max')
        if target!='budget_max' and s.profile.get('rooms') is None and (s.research_query or {}).get('group_by')=='rooms':addressed.append('rooms')
    elif target:addressed.append(target)
    if not addressed:return
    s.guidance=deepcopy(s.guidance)
    assisted=s.guidance.setdefault('assisted',{})
    for field in addressed:assisted[field]=scope(s,field)
    s.guidance['followup']={'field':target or addressed[0],'seq':s.request_review_seq}


def question(s):
    step=next_step(s)
    if step is None:return None
    mode,field=step
    if field=='budget_max':
        text=('С учётом этих ориентиров какой верхний бюджет покупки вам комфортен? Можно пока оставить его неопределённым.'
              if mode=='followup' and s.market else
              'На какую сумму покупки ориентируемся? Если бюджет пока не определён, помогу разобраться по ценам квартир.')
        options=['Пока не знаю','Без ограничений']
    else:
        text=('Какая комнатность подходит после сравнения вариантов в вашем бюджете? Можно пока не выбирать.'
              if mode=='followup' and s.market else
              'Сколько комнат вам хотелось бы? Если пока не знаете, помогу сравнить варианты по вашему бюджету и пожеланиям.')
        options=['1','2','3','Пока не знаю','Любые']
    return {'question':text,'options':options,'field':field}
