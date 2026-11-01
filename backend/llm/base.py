"""Единый интерфейс LLM для всех провайдеров (DeepSeek, GigaChat, офлайн-mock).

Агент работает только с этим интерфейсом: сообщения в формате
{"role": "system|user|assistant", "content": "..."}, ответ — LLMResponse.
Провайдер сам переводит формат в свой API (например, tools -> functions для GigaChat).
"""
from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Ошибка обращения к модели (сеть, авторизация, формат)."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""  # чтобы вернуть результат именно этому вызову (сообщение role=tool, tool_call_id)


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""


@dataclass
class Tool:
    """Описание инструмента в нейтральном формате (JSON Schema параметров)."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMClient(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Tool] | None = None,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 800,
        tag: str = "",
    ) -> LLMResponse:
        """Один ход диалога. messages — в формате OpenAI: system / user / assistant (в т. ч. с tool_calls) / tool.
        tag — метка шага для логов."""

    async def chat_json(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.2, tag: str = "", **kw
    ) -> dict[str, Any]:
        """Ответ модели в виде JSON-объекта. Устойчив к ```json-обёрткам и тексту вокруг."""
        resp = await self.chat(messages, json_mode=True, temperature=temperature, tag=tag, **kw)
        return parse_json_loose(resp.text)


def parse_json_loose(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Модель вернула не JSON: {text[:200]}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Модель вернула не JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise LLMError("Ожидался JSON-объект")
    return data


RETRY_STATUSES = {429, 500, 502, 503, 504}


async def post_with_retry(http, url: str, *, attempts: int = 3, provider: str = "LLM", **kw):
    """POST с повтором при сетевых сбоях и 429/5xx (backoff 0.5 s, 1 s)."""
    import httpx

    last: Exception | None = None
    for i in range(attempts):
        try:
            r = await http.post(url, **kw)
            if r.status_code not in RETRY_STATUSES or i == attempts - 1:
                return r
            last = LLMError(f"{provider} {r.status_code}")
        except httpx.TransportError as exc:  # таймауты, обрыв keep-alive, DNS
            last = exc
        await asyncio.sleep(0.5 * (i + 1))
    raise LLMError(f"{provider} недоступен: {type(last).__name__} {last}".strip())
