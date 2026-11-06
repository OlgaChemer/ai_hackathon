"""Допуски инструментов и безопасное продолжение поиска из одного состояния."""

from .criteria import selectable
from .profile import missing_search,missing_required
from .capabilities import programs_current, decision


def available(s, name: str) -> bool:
    return decision(s,name)[0]


def next_search_action(s):
    """Только явное рабочее намерение разрешает продолжение без нового ответа клиента."""
    if s.requested_action and available(s,s.requested_action):return s.requested_action
    if s.request_intent=='call':
        for name in ('confirm_red_lines','ask_payment'):
            if available(s,name):return name
        return None
    if s.request_intent=='selection' and s.selection_operation in ('compare','analyze'):
        if s.selection_query.get('scope')!='shown' and s.search is None:
            return 'search_listings' if available(s,'search_listings') else None
        return 'show_selection' if s.selection_dirty and available(s,'show_selection') else None
    if s.request_intent not in ("search", "refine", "selection", "continue") or missing_search(
        s.profile
    ):
        return None
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
