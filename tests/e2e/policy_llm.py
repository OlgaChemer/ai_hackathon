"""Модель-«дублёр» для сквозной проверки без сети: ведёт агента по обычному ходу сделки правилами.

Это НЕ режим продукта (в продукте без настоящей модели агент не работает) — только для e2e-прогона
интерфейса и действий, когда DeepSeek/GigaChat недоступны: она вызывает те же действия (tools),
что и настоящая модель, видит их результаты и состояние сделки из системного промпта.
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.llm import LLMClient, LLMResponse, ToolCall
from backend.sources.base import normalize_city


QUESTION = re.compile(r"^\s*(?:а\s+|и\s+|скажи(?:те)?,?\s+)?(?:что|как|почему|зачем|сколько|когда|где|какой|какая|какое|какие|кто|чем|чего|откуда|можно ли|нужно ли|надо ли|правда ли|объясни|расскажи|подскажи)\b", re.I)

def is_question(text):
    """Только эвристика дублёра для тестов интерфейса; в продукте классифицирует модель."""
    return bool(QUESTION.search(text.replace('ё','е')))


def _call(name: str, **args) -> LLMResponse:
    return LLMResponse(text="", tool_calls=[ToolCall(name=name, arguments=args, id=f"call_{name}_{id(args)}")])


def parse_request(text: str) -> dict:
    t = text.lower().replace("ё", "е")
    out: dict[str, Any] = {}
    city = normalize_city(text)
    if city:
        out["city"] = city
    m = re.search(r"(\d)\s*-?\s*(?:комн|к\b|х\s*комн)", t)
    rooms = int(m.group(1)) if m else None
    for word, n in (("студи", 0), ("однушк", 1), ("двушк", 2), ("трешк", 3), ("трехкомн", 3), ("двухкомн", 2), ("однокомн", 1)):
        if word in t:
            rooms = n
    if rooms is not None:
        out["rooms"] = rooms
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(млн|миллион)", t)
    if m:
        out["budget_max"] = int(float(m.group(1).replace(",", ".")) * 1_000_000)
    if "наличн" in t:
        out["payment"] = "cash"
    elif "копим" in t or "коплю" in t:
        out["payment"] = "mortgage_saving"
    elif "ипотек" in t:
        out["payment"] = "mortgage_ready"
    if "новостро" in t and "вторич" in t:
        out["housing"] = "both"
    elif "новостро" in t:
        out["housing"] = "new"
    elif "вторич" in t:
        out["housing"] = "secondary"
    if "детей нет" in t or "без детей" in t:
        out["children"] = 0
    m = re.search(r"(сын|дочь|дочка|ребенок|ребенку)\D{0,10}(\d{1,2})\s*(год|лет)", t)
    if m:
        out["children"], out["youngest_age"] = 1, int(m.group(2))
    if "маткапитал" in t:
        out["matcap"] = True
    return out


def _state(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        try:
            data = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("source") == "session_state":
            return data["data"]
    return ""


def _profile(state: str) -> dict:
    m = re.search(r"Запрос: (\{.*\})", state)
    return json.loads(m.group(1)) if m else {}


def _last_tool(messages: list[dict]) -> tuple[str | None, dict]:
    """Имя и результат последнего действия, если последнее сообщение — результат действия."""
    if not messages or messages[-1]["role"] != "tool":
        return None, {}
    call_id = messages[-1]["tool_call_id"]
    for m in reversed(messages):
        for c in m.get("tool_calls") or []:
            if c["id"] == call_id:
                return c["function"]["name"], json.loads(messages[-1]["content"])
    return None, {}


def _done(messages: list[dict], name: str) -> bool:
    return any(c["function"]["name"] == name for m in messages for c in (m.get("tool_calls") or []))


class PolicyLLM(LLMClient):
    name = "policy"
    model = "e2e-rules"

    async def chat(self, messages: list[dict[str, Any]], *, tools=None, json_mode: bool = False, tag: str = "", **kw) -> LLMResponse:
        if tag == "answer" or (messages and messages[0]["role"] == "system" and "Клиент задал вопрос" in messages[0]["content"]):
            return LLMResponse(text="Сервис безопасных расчётов: деньги уходят продавцу только после регистрации права. "
                                    "Возвращаемся к текущему шагу.")
        if json_mode:
            return LLMResponse(text=json.dumps(await self.chat_json(messages, tag=tag)))
        if tools is None:
            return LLMResponse(text="Готово. Напишите, что дальше.")
        return self.decide(messages)

    def decide(self, messages: list[dict]) -> LLMResponse:
        state = _state(messages)
        prof = _profile(state)
        last_name, last = _last_tool(messages)
        if messages[-1]["role"] == "user":  # новое сообщение клиента — записать, что в нём есть
            text = messages[-1]["content"]
            if is_question(text):
                return LLMResponse(text="Отвечаю: деньги проходят через сервис безопасных расчётов. Продолжаем?")
            parsed = parse_request(text)
            if parsed:
                return _call("update_request", **parsed)
        if last_name == "ask_client" and last.get("answer"):
            parsed = parse_request(last["answer"])
            if parsed:
                return _call("update_request", **parsed)
        if last.get("error"):
            return LLMResponse(text=f"Не получилось: {last['error']}")
        missing = [k for k in ("city", "rooms", "budget_max") if prof.get(k) in (None, "")]
        if missing:
            if last_name == "ask_client":
                return LLMResponse(text="Напишите, пожалуйста, город, число комнат и бюджет.")
            return _call("ask_client", question="Уточните, пожалуйста: в каком городе, сколько комнат и какой бюджет?",
                         options=["Казань", "Москва", "2 комнаты", "до 9 млн"])
        if "Рамки клиентом НЕ подтверждены" in state:
            if last_name == "confirm_red_lines" and not last.get("approved"):
                return LLMResponse(text="Хорошо, что поменять в запросе?")
            return _call("confirm_red_lines")
        if not prof.get("payment"):
            return _call("ask_payment")
        if prof["payment"] != "cash" and prof.get("children") is None:
            return _call("ask_family")
        if not _done(messages, "mortgage_programs"):
            return _call("mortgage_programs")
        if prof["payment"] == "mortgage_saving" and not _done(messages, "savings_plan"):
            return _call("savings_plan")
        if "Поиск: найдено" not in state:
            return _call("search_listings")
        if last_name == "search_listings" and not last.get("selectable", last.get("in_budget")):
            return LLMResponse(text="Подходящих квартир нет — давайте поменяем бюджет или город.")
        if last_name == "show_selection":
            if last.get("action") == "proceed":
                target = (last.get("liked") or last.get("shown") or [{}])[0].get("id")
                return _call("call_seller", listing_id=target)
            if last.get("action") == "text" and last.get("client_said"):
                parsed = parse_request(last["client_said"])
                if parsed:
                    return _call("update_request", **parsed)
            return _call("show_selection")
        if "Последняя подборка" not in state:
            return _call("show_selection")
        calls = re.findall(r"Звонок (\S+): ([^\n]+)", state)
        if last_name == "call_seller" and last.get("called"):
            return _call("confirm_price", listing_id=calls[-1][0].rstrip(":") if calls else "")
        if calls:
            lid, info = calls[-1]
            if "цену подтвердил клиент" in info and "квартира выбрана" not in info and not _done(messages, "plan_viewing"):
                return _call("plan_viewing", listing_id=lid)
            if "квартира выбрана" in info and not _done(messages, "prepare_deal"):
                return _call("prepare_deal", listing_id=lid)
            if _done(messages, "prepare_deal"):
                return LLMResponse(text="Сделка оформлена. Поздравляю! Если нужно — помогу с переездом.")
        return _call("show_selection")

    async def chat_json(self, messages: list[dict[str, Any]], *, tag: str = "", **kw) -> dict[str, Any]:
        if tag == 'request_audit':
            from tests.fakes import audit_reply
            return audit_reply(messages)
        if tag == 'request_review':
            from tests.fakes import review_reply
            payload = json.loads(next(m['content'] for m in messages if m['role'] == 'user'))['data']
            return review_reply(payload)
        sys_text = messages[0]["content"] if messages else ""
        user = messages[-1]["content"] if messages else ""
        try:
            context = json.loads(user).get("data", {})
            if isinstance(context, dict):
                user = context.get("conversation", context.get("transcript", user))
            elif isinstance(context, str):
                user = context
        except (ValueError, AttributeError):
            context = {}
        num = lambda pat, s: int(m.group(1)) if (m := re.search(pat, s)) else None  # noqa: E731
        if tag == "buyer_turn":
            authority = num(r"больше (\d+) ₽", sys_text) or 0
            target = num(r"Цель — (\d+) ₽", sys_text) or authority
            ask = num(r"Текущая цена продавца: (\d+)", user) or authority
            last = num(r"последнее предложение: (\d+)", user)
            if "знакомство" in user:
                return {"utterance": "Здравствуйте! Я ИИ-ассистент покупателя из Домклика, разговор записывается. Квартира продаётся?",
                        "offer": None, "accept": False}
            if "ЭТАП: просмотр" in user:
                slots = re.search(r"время (.+?) и спроси", user)
                return {"utterance": f"Давайте договоримся о просмотре: {slots.group(1) if slots else 'в выходные'}?",
                        "offer": None, "accept": False}
            if ask <= authority and (last is None or ask - last <= 50_000):
                return {"utterance": "Хорошо, договорились.", "offer": ask, "accept": True}
            offer = min(authority, (last or int(target * 0.98)) + 50_000)
            return {"utterance": f"Готов предложить {offer:,} ₽.".replace(",", " "), "offer": offer, "accept": False}
        if tag == "seller_turn":
            floor = num(r"ниже (\d+) ₽", sys_text) or 0
            ask = num(r"Твоя текущая цена: (\d+)", user) or floor
            offer = num(r"предложение покупателя: (\d+)", user)
            if offer is None:
                return {"utterance": "Да, продаётся. Ремонт косметический, один собственник.", "ask": ask, "accept": False}
            new_ask = max(floor, ask - 60_000)
            return {"utterance": "Давайте в субботу, могу немного уступить.", "ask": new_ask, "accept": offer >= new_ask}
        if tag == "extract_call":
            labels = context.get("labels", [])
            return {"facts": {lab: "уточнено у продавца" for lab in labels[:3]}, "viewing": None, "corrections": {}}
        if tag == "parse_seller":
            m = re.search(r"(\d[\d\s]{5,})", user)
            return {"ask": int(re.sub(r"\D", "", m.group(1))) if m else None, "accept": "соглас" in user.lower()}
        return {}
