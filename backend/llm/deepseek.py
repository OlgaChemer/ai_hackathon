"""DeepSeek: OpenAI-совместимый Chat Completions API."""
from __future__ import annotations

import json

import httpx

from .base import LLMClient, LLMError, LLMResponse, Tool, ToolCall, post_with_retry


class DeepSeekClient(LLMClient):
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0):
        if not api_key:
            raise LLMError("DEEPSEEK_API_KEY не задан")
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def chat(
        self,
        messages,
        *,
        tools: list[Tool] | None = None,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 800,
        tag: str = "",
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        r = await post_with_retry(self._http, "/chat/completions", json=body, provider="DeepSeek")
        if r.status_code != 200:
            raise LLMError(f"DeepSeek {r.status_code}: {r.text[:300]}")

        msg = r.json()["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, id=c.get("id") or ""))
        return LLMResponse(
            text=msg.get("content") or "", tool_calls=calls, provider=self.name, model=self.model
        )
