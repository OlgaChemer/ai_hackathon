"""Поддельные модели для тестов без сети. В продукте их нет: без настоящей модели агент не работает."""
from __future__ import annotations

import json
from typing import Any, Callable

from backend.llm import LLMClient, LLMError, LLMResponse, ToolCall


def call(name: str, **args) -> LLMResponse:
    """Ответ модели «выполни действие name(args)»."""
    return LLMResponse(text="", tool_calls=[ToolCall(name=name, arguments=args, id=f"call_{name}")])


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text)


class ScriptedLLM(LLMClient):
    """Модель агента по сценарию: каждый ход — следующий шаг сценария (действие или текст клиенту).
    Шаг может быть функцией от сообщений (видит результаты действий). Сценарий кончился — короткий текст."""
    name = "fake"
    model = "scripted"

    def __init__(self, *steps: LLMResponse | Callable[[list[dict]], LLMResponse], fail: bool = False):
        self.steps = list(steps)
        self.fail = fail
        self.seen: list[list[dict]] = []  # что модель видела на каждом ходу

    async def chat(self, messages: list[dict[str, Any]], **kw) -> LLMResponse:
        if self.fail:
            raise LLMError("сеть недоступна")
        self.seen.append(messages)
        if kw.get("tools") is None or not self.steps:  # лимит действий подряд или сценарий кончился
            return say("Готово, жду вас.")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


def last_result(messages: list[dict]) -> dict:
    """Результат последнего действия, который видит модель."""
    return json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])


class NegotiationLLM(LLMClient):
    """Ходы звонка: покупатель поднимает предложение шагами к цели, продавец уступает к своей минимальной цене."""
    name = "fake"
    model = "negotiation"

    def __init__(self, n):
        self.n = n

    async def chat(self, messages, **kw):
        raise LLMError("в звонке используется chat_json")

    async def chat_json(self, messages, *, tag: str = "", **kw) -> dict:
        n = self.n
        if tag == "buyer_turn":
            phase = "clarify" if "знакомство" in messages[1]["content"] else "viewing" if "ЭТАП: просмотр" in messages[1]["content"] else "bargain"
            if phase == "clarify":
                return {"utterance": "Здравствуйте! Я ИИ-ассистент покупателя из Домклика, разговор записывается. Квартира продаётся?",
                        "offer": None, "accept": False}
            if phase == "viewing":
                return {"utterance": f"Давайте договоримся о просмотре: {n.slots[0]} или {n.slots[1]}?", "offer": None, "accept": False}
            offer = min(n.authority, (n.offer or int(n.target * 0.97)) + 50_000)
            if n.asking <= n.authority and n.asking - (n.offer or 0) <= 40_000:
                return {"utterance": "Хорошо, договорились.", "offer": n.asking, "accept": True}
            return {"utterance": "Предлагаю чуть выше.", "offer": offer, "accept": False}
        if tag == "seller_turn":
            if n.offer is None:
                return {"utterance": "Да, продаётся, ремонт косметический.", "ask": n.asking, "accept": False}
            ask = max(n.seller["min_price"], n.asking - 60_000)
            return {"utterance": "Могу немного уступить.", "ask": ask, "accept": n.offer >= ask}
        if tag == "extract_call":
            return {"facts": {q["label"]: "уточнено" for q in n.questions}, "viewing": None, "corrections": {}}
        if tag == "parse_seller":
            return {"ask": None, "accept": False}
        raise LLMError(f"неизвестный шаг {tag}")


async def drive(s, answers: dict | None = None, until: Callable[[dict], bool] = lambda ev: False, timeout: float = 5) -> list[dict]:
    """Запускает агента и играет клиента: на карточки отвечает по answers {kind: (approved, data)} (по умолчанию «да»),
    пока until(событие) не вернёт True или агент не станет ждать сообщения. Возвращает события ленты."""
    import asyncio

    from backend.agent.loop import run_session

    answers = answers or {}
    q: asyncio.Queue = asyncio.Queue()
    s.subscribers.add(q)
    s.task = asyncio.create_task(run_session(s))
    events: list[dict] = []
    try:
        while True:
            ev = await asyncio.wait_for(q.get(), timeout)
            if ev["type"] == "typing":
                if not ev["on"] and not s.pending and s.inbox.empty() and _waiting_for_client(s):
                    while not q.empty():  # агент мог уйти вперёд — забираем всё, что уже в очереди
                        late = q.get_nowait()
                        if late["type"] != "typing":
                            events.append(late)
                    break
                continue
            events.append(ev)
            if ev["type"] == "confirm":
                approved, data = answers.get(ev["kind"], (True, {}))
                s.resolve(ev["kind"], approved, data)
            if until(ev) or ev["type"] == "error":
                break
    finally:
        s.task.cancel()
    return events


def _waiting_for_client(s) -> bool:
    """Агент написал клиенту текст и ждёт ответа (последнее в памяти — его реплика без действий)."""
    last = s.messages[-1] if s.messages else {}
    return last.get("role") == "assistant" and not last.get("tool_calls")
