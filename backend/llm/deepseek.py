"""DeepSeek: OpenAI-совместимый Chat Completions API."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx

from .base import LLMClient, LLMError, LLMResponse, Tool, ToolCall, post_with_retry
from .. import runtime


class DeepSeekClient(LLMClient):
    name = "deepseek"

    async def analyze_images(self,prompt,images):
        content=[{'type':'text','text':prompt},*[{'type':'image_url','image_url':{
            'url':'data:'+image.mime_type+';base64,'+base64.b64encode(image.data).decode()}} for image in images]]
        return await self.chat([{'role':'user','content':content}],json_mode=True,max_tokens=2200,temperature=.1,tag='workshop_vision',thinking=False)

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0):
        if not api_key:
            raise LLMError("DEEPSEEK_API_KEY не задан")
        self.model = model
        self._call_timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def chat(self, messages, **kwargs) -> LLMResponse:
        with runtime.timed("llm", provider=self.name, model=self.model, tag=kwargs.get("tag", "")):
            try:
                return await asyncio.wait_for(self._chat(messages, **kwargs), self._call_timeout)
            except asyncio.TimeoutError:
                raise LLMError("Истекло время ответа DeepSeek") from None

    async def _chat(
        self,
        messages,
        *,
        tools: list[Tool] | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
        temperature: float = 0.3,
        max_tokens: int = 800,
        tag: str = "",
        thinking: bool | None = None,
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": [{k: v for k, v in m.items() if k not in ("functions_state_id", "provider_state")} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking is not None:body['thinking']={'type':'enabled' if thinking else 'disabled'}
        if json_mode or json_schema is not None:
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
