"""Исполняемые правила возможностей; общий источник для модели, кода и FAQ."""
from dataclasses import dataclass

from . import finance
from .profile import missing_required,missing_search

CORE_FIELDS=frozenset(('city','rooms','budget_max','okrug','housing','rooms_options','cities','districts','prefer_center','center_required'))
FILTER_FIELDS=frozenset(('features','poi','kitchen_min','area_min','not_first_floor','not_last_floor',
                        'condition','material','year_min','metro_max_minutes','soft_criteria',
                        'clear_criteria','avoid_districts','filters','wishes'))


@dataclass(frozen=True)
class Policy:
    requires: tuple[str,...]
    invalidates: tuple[str,...]=()
    units: tuple[tuple[str,str],...]=()
    information: bool=False


POLICIES={
    'update_request':Policy(('new_input',),('search','selection','agreements','finance')),
    'ask_client':Policy(('can_clarify',)),
    'market_prices':Policy(('search_scope',),information=True),
    'confirm_red_lines':Policy(('budget_known','limit_needed')),
    'ask_payment':Policy(('payment_needed',),('finance',)),
    'ask_family':Policy(('family_needed',),('finance',)),
    'ask_housing':Policy(('housing_needed',),('search','selection')),
    'mortgage_programs':Policy(('programs_needed','budget_known','payment','family_or_cash'),information=True),
    'savings_plan':Policy(('budget_known','saving_payment'),information=True),
    'affordable_budget':Policy(('affordability_requested',),information=True),
    'search_listings':Policy(('search_ready',),('selection',)),
    'show_selection':Policy(('selection_source',)),
    'offer_search_alternatives':Policy(('search',)),
    'probe_budget':Policy(('search','budget_known'),units=(('budget_max','RUB'),)),
    'call_seller':Policy(('search','search_scope','shown','call_consent','fresh_selection','budget_known','limit','payment'),units=(('listing_id','listing_id'),)),
    'confirm_price':Policy(('calls','limit'),units=(('listing_id','listing_id'),)),
    'plan_viewing':Policy(('agreed','limit'),units=(('listing_id','listing_id'),)),
    'prepare_deal':Policy(('chosen','limit','finance_ready','deal_not_done'),units=(('listing_id','listing_id'),)),
}

REASONS={
    'search_scope':'Для поиска нужен город.', 'budget_known':'Для этого действия нужен конкретный лимит покупки.',
    'new_input':'Нет новых условий для применения.', 'can_clarify':'Исходных данных достаточно; переходите к подбору.',
    'city_rooms':'Нужны город и число комнат.', 'profile_ready':'Нужны город, комнаты и бюджет.',
    'limit_needed':'Этот лимит уже подтверждён.', 'payment_needed':'Способ оплаты уже известен.',
    'family_needed':'Сведения о семье уже известны либо оплата своими средствами.',
    'housing_needed':'Рынок жилья уже известен.', 'programs_needed':'Расчёт программ уже актуален.',
    'payment':'Сначала уточните способ оплаты.', 'family_or_cash':'Для ипотечного расчёта нужны сведения о семье.',
    'saving_payment':'План накоплений нужен при накоплении взноса.', 'search_ready':'Для поиска нужны профиль, подтверждённый лимит и способ оплаты.',
    'selection_source':'Сначала получите объявления или восстановите показанную подборку.',
    'search':'Сначала выполните поиск.', 'shown':'Сначала покажите квартиры покупателю.',
    'call_consent':'Нужна явная просьба звонить по конкретной квартире.',
    'fresh_selection':'Подборку нужно обновить под изменённые условия.',
    'calls':'Сначала нужны результаты переговоров.', 'limit':'Сначала подтвердите лимит покупки.',
    'agreed':'Сначала покупатель подтверждает итоговую цену.', 'chosen':'Сначала покупатель выбирает квартиру после просмотра.',
    'finance_ready':'Сначала подготовьте финансовый расчёт.', 'deal_not_done':'План уже подтверждён.',
    'affordability_requested':'Расчёт по личным доходам нужен по явной просьбе клиента. Для средней цены квартир используйте market_prices.',
}


def programs_current(s):
    return s.programs_result is not None and s.programs_key==finance.programs_key(s.profile)


def decision(s,name):
    policy=POLICIES.get(name)
    if policy is None:return False,'Для этого инструмента не определена политика исполнения.'
    if s.request_intent=='hold' and name!='update_request' and not policy.information:return False,'Клиент приостановил действия.'
    if (s.request_intent=='call' and s.call_targets and not s.calls and not s.selection_profile_stale
            and name not in ('update_request','call_seller','confirm_red_lines','ask_client','ask_payment')):
        return False,'Сначала выполните явную просьбу о звонке по выбранному объекту.'
    p=s.profile;ready=not missing_required(p)
    finance_ready=p.get('payment')=='cash' or (s.mortgage is not None and (p.get('payment')!='mortgage_saving' or s.finance is not None))
    work=s.request_intent in (None,'search','refine','selection','call','seller_questions','continue')
    tests={
        'new_input':not s.request_inputs or any(x['seq']>s.request_review_seq for x in s.request_inputs),
        'can_clarify':(not ready or s.search is not None) and (s.request_intent!='question' or bool(s.requested_action)),
        'city_rooms':bool(p.get('city') and p.get('rooms') is not None),
        'profile_ready':ready,'limit_needed':not s.red_lines_ok or s.requested_action=='confirm_red_lines',
        'payment_needed':not p.get('payment') or s.requested_action=='ask_payment',
        'family_needed':(p.get('children') is None and p.get('payment')!='cash') or s.requested_action=='ask_family',
        'housing_needed':not p.get('housing') or s.requested_action=='ask_housing',
        'programs_needed':not programs_current(s),'payment':bool(p.get('payment')),
        'family_or_cash':p.get('payment')=='cash' or p.get('children') is not None,
        'saving_payment':p.get('payment')=='mortgage_saving',
        'search_scope':not missing_search(p),'budget_known':p.get('budget_max') is not None,
        'search_ready':not missing_search(p) and work,
        'selection_source':(bool(s.shown) if s.selection_query.get('scope')=='shown' else s.search is not None)
            if s.selection_operation in ('compare','analyze') else s.search is not None,
        'search':s.search is not None,'shown':bool(s.shown),'call_consent':bool(s.call_targets),
        'fresh_selection':not s.selection_profile_stale,'calls':bool(s.calls),'limit':s.red_lines_ok,
        'agreed':bool(s.agreed),'chosen':bool(s.chosen),'finance_ready':finance_ready,'deal_not_done':s.status.get(8)!='done',
        'affordability_requested':s.requested_action=='affordable_budget',
    }
    for requirement in policy.requires:
        if not tests.get(requirement,False):return False,REASONS.get(requirement,'Условие исполнения не выполнено.')
    return True,None


def catalog(s=None):
    from . import actions
    actions._load()
    rows=[]
    for a in actions.REGISTRY.values():
        policy=POLICIES.get(a.name)
        allowed,reason=decision(s,a.name) if s is not None else (policy is not None,None)
        rows.append({'name':a.name,'description':a.description,'arguments':a.parameters,
                     'allowed':allowed,'reason':reason,'requires':list(policy.requires) if policy else [],
                     'invalidates':list(policy.invalidates) if policy else [],'units':dict(policy.units) if policy else {},
                     'information':bool(policy and policy.information)})
    return rows


def argument_denial(s,name,arguments):
    """Ограничения конкретного объекта — та же политика при исполнении."""
    policy=POLICIES.get(name)
    if policy is None:return 'Для инструмента не определена политика.'
    if dict(policy.units).get('listing_id')!='listing_id':return None
    lid=arguments.get('listing_id')
    x=next((x for x in (s.search or {}).get('listings',[]) if x['id']==lid),None)
    if x is None:return 'Объекта нет в результатах текущего поиска.'
    from .criteria import evaluate
    if evaluate(s.profile,x)['status']=='rejected':return 'Объект нарушает обязательные условия.'
    if name=='call_seller':
        if lid not in s.call_targets:return 'Нужна явная просьба звонить по этой квартире.'
        if lid not in s.prefs.liked and not any(x['id']==lid for x in s.shown):return 'Сначала покажите эту квартиру покупателю.'
    if name=='confirm_price' and lid not in s.calls:return 'По этой квартире ещё нет результата переговоров.'
    if name=='plan_viewing' and lid not in s.agreed:return 'Покупатель ещё не подтвердил цену этой квартиры.'
    if name=='prepare_deal' and s.chosen!=lid:return 'Покупатель ещё не выбрал эту квартиру.'
    return None
