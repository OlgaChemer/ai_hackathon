"""Допуски инструментов и безопасное продолжение поиска из одного состояния."""

from .criteria import selectable
from .profile import missing_search,missing_required
from .capabilities import programs_current, decision


def available(s, name: str) -> bool:
    return decision(s,name)[0]


def pending_calls(s):
    attempted={op.get('arguments',{}).get('listing_id') for op in s.operations.values()
               if op.get('name')=='call_seller' and op.get('request_seq')==s.request_review_seq
               and op.get('status') in ('completed','needs_review','external_started','external_completed')}
    return [lid for lid in s.call_targets if lid not in s.calls and lid not in attempted]


def next_finance_action(s):
    """Ипотечная анкета имеет тот же порядок при подборе и перед звонком."""
    if s.profile.get('payment') not in ('mortgage_ready','mortgage_saving') or not s.profile.get('budget_max'):
        return None
    for name in ('ask_family','ask_deposit','mortgage_programs'):
        if available(s,name):return name
    if s.profile.get('payment')=='mortgage_saving' and s.finance is None and available(s,'savings_plan'):
        return 'savings_plan'
    return None


def next_search_action(s):
    """Только явное рабочее намерение разрешает продолжение без нового ответа клиента."""
    if s.requested_action and available(s,s.requested_action):return s.requested_action
    from . import guidance
    step=guidance.next_step(s)
    if s.request_intent=='question' and step and step[0]=='followup':return 'ask_client'
    if s.request_intent=='call':
        for name in ('confirm_red_lines','ask_payment'):
            if available(s,name):return name
        finance_action=next_finance_action(s)
        if finance_action:return finance_action
        if available(s,'financing_choice'):return 'financing_choice'
        if available(s,'call_seller') and pending_calls(s):return 'call_seller'
        return None
    if s.request_intent=='selection' and s.selection_operation in ('compare','analyze'):
        if s.selection_query.get('scope')!='shown' and s.search is None:
            return 'search_listings' if available(s,'search_listings') else None
        return 'show_selection' if s.selection_dirty and available(s,'show_selection') else None
    if s.request_intent not in ("search", "refine", "selection", "continue") or missing_search(
        s.profile
    ):
        return None
    step=guidance.next_step(s)
    if step:return 'market_prices' if step[0]=='help' else 'ask_client'
    # Для уже полностью сформулированной покупки сохраняем начальную карточку
    # лимита. Исследования и широкий поиск не требуют заполнить этот профиль.
    if not missing_required(s.profile) and available(s,'confirm_red_lines'):return 'confirm_red_lines'
    if s.red_lines_ok and not s.profile.get('_extras_asked') and available(s,'ask_extras'):return 'ask_extras'
    if s.profile.get('budget_max'):
        finance_action=next_finance_action(s)
        if finance_action:return finance_action
    if s.search is None:
        return "search_listings" if available(s, "search_listings") else None
    if not s.shown or s.selection_dirty:
        return (
            "show_selection"
            if any(selectable(x, s.prefs) for x in s.search["pool"])
            else "offer_search_alternatives"
        )
    return None


def planned_call(s,name):
    """Аргументы принудительного продолжения берутся только из проверенного состояния."""
    import uuid
    from ..llm import ToolCall
    args={}
    if name=='call_seller':args['listing_id']=pending_calls(s)[0]
    elif name=='ask_client':
        from . import guidance
        args=guidance.question(s) or {}
    return ToolCall(name,args,f'progress_{uuid.uuid4().hex[:8]}')
