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


def next_search_action(s):
    """Только явное рабочее намерение разрешает продолжение без нового ответа клиента."""
    if s.requested_action and available(s,s.requested_action):return s.requested_action
    if s.request_intent=='call':
        for name in ('confirm_red_lines','ask_payment'):
            if available(s,name):return name
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
    from . import guidance
    step=guidance.next_step(s)
    if step:return 'market_prices' if step[0]=='help' else 'ask_client'
    # Для уже полностью сформулированной покупки сохраняем начальную карточку
    # лимита. Исследования и широкий поиск не требуют заполнить этот профиль.
    if not missing_required(s.profile) and available(s,'confirm_red_lines'):return 'confirm_red_lines'
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
