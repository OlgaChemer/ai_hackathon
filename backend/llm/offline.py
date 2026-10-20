"""Офлайн-режим: модель недоступна.

Клиент всегда бросает LLMError, а каждый шаг агента в этом случае использует
детерминированный запасной вариант (регулярные выражения, сценарный торг).
Так демо проходит целиком даже без интернета.
"""
from __future__ import annotations

from .base import LLMClient, LLMError, LLMResponse


class OfflineClient(LLMClient):
    name = "offline"
    model = "rules"

    def __init__(self, reason: str = "LLM_PROVIDER=mock"):
        self.reason = reason

    async def chat(self, messages, **kw) -> LLMResponse:
        raise LLMError(f"офлайн-режим: {self.reason}")
