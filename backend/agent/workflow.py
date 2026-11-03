"""Допуски инструментов и безопасное продолжение поиска из одного состояния."""

from . import finance
from .profile import missing_required
from .criteria import selectable


def programs_current(s):
    return s.programs_result is not None and s.programs_key == finance.programs_key(
        s.profile
    )


def available(s, name: str) -> bool:
    """Модель выбирает действия среди разрешённых текущим состоянием, порядок не диктуется."""
    p = s.profile
    if s.request_intent == "hold" and name not in ("update_request", "ask_client"):
        return False
    work_intent = s.request_intent in (None, "search", "refine", "continue")
    ready = not missing_required(p)
    finance_ready = p.get("payment") == "cash" or (
        s.mortgage is not None
        and (p.get("payment") != "mortgage_saving" or s.finance is not None)
    )
    search_ready = ready and s.red_lines_ok and bool(p.get("payment")) and work_intent
    rules = {
        "update_request": not s.request_inputs
        or any(x["seq"] > s.request_review_seq for x in s.request_inputs),
        # Подтверждение рамок завершает вводную анкету. Если поиск ничего не даст,
        # снова разрешаем обсудить изменение условий.
        "ask_client": not ready or not s.red_lines_ok or s.search is not None,
        "confirm_red_lines": ready and not s.red_lines_ok,
        "market_prices": bool(p.get("city") and p.get("rooms") is not None),
        "ask_payment": not p.get("payment"),
        "ask_housing": not p.get("housing"),
        "ask_family": p.get("children") is None and p.get("payment") != "cash",
        "mortgage_programs": not programs_current(s)
        and ready
        and bool(p.get("payment"))
        and (p.get("payment") == "cash" or p.get("children") is not None),
        "savings_plan": ready and p.get("payment") == "mortgage_saving",
        "search_listings": search_ready,
        "show_selection": s.search is not None,
        "offer_search_alternatives": s.search is not None,
        "probe_budget": s.search is not None,
        "call_seller": s.search is not None and search_ready and bool(s.shown),
        "confirm_price": bool(s.calls) and s.red_lines_ok,
        "plan_viewing": bool(s.agreed) and s.red_lines_ok,
        "prepare_deal": bool(s.chosen)
        and s.red_lines_ok
        and finance_ready
        and s.status.get(8) != "done",
    }
    return bool(rules.get(name, True))


def next_search_action(s):
    """Только явное рабочее намерение разрешает продолжение без нового ответа клиента."""
    if s.request_intent not in ("search", "refine", "continue") or missing_required(
        s.profile
    ):
        return None
    for name in ("confirm_red_lines", "ask_payment"):
        if available(s, name):
            return name
    if s.search is None:
        return "search_listings" if available(s, "search_listings") else None
    if not s.shown:
        return (
            "show_selection"
            if any(selectable(x, s.prefs) for x in s.search["pool"])
            else "offer_search_alternatives"
        )
    return None
