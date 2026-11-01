"""GigaChat: OAuth-токен + Chat Completions.

Отличия от OpenAI-формата, которые закрывает этот модуль:
- токен получаем по ключу авторизации (Basic) на отдельном адресе и обновляем
  за минуту до истечения (живёт ~30 минут);
- инструменты передаются как `functions`, а вызов приходит в `function_call`
  (arguments — уже объект, а не строка);
- нет response_format: JSON просим в системном промпте;
- сертификат НУЦ Минцифры: укажите GIGACHAT_CA_BUNDLE или GIGACHAT_VERIFY_SSL=false.
"""
from __future__ import annotations

import json
import time
import uuid

import httpx

from .base import LLMClient, LLMError, LLMResponse, Tool, ToolCall, post_with_retry

JSON_HINT = "Ответь строго одним JSON-объектом без пояснений и без markdown."


def to_gigachat_functions(tools: list[Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]


def to_gigachat_messages(messages: list[dict], json_mode: bool) -> list[dict]:
    """Формат OpenAI -> GigaChat: вызов действия — function_call у ассистента, результат — сообщение role=function."""
    out = []
    names: dict[str, str] = {}  # id вызова -> имя функции (у GigaChat результат подписывается именем)
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            call = m["tool_calls"][0]  # GigaChat вызывает одну функцию за ход
            names[call["id"]] = call["function"]["name"]
            args = call["function"]["arguments"]
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "function_call": {"name": call["function"]["name"],
                                          "arguments": json.loads(args) if isinstance(args, str) else args}})
        elif m.get("role") == "tool":
            out.append({"role": "function", "name": names.get(m.get("tool_call_id"), m.get("name", "")), "content": m["content"]})
        else:
            out.append({k: v for k, v in m.items() if k in ("role", "content")})
    if json_mode:
        if out and out[0]["role"] == "system":
            out[0]["content"] = f"{out[0]['content']}\n\n{JSON_HINT}"
        else:
            out.insert(0, {"role": "system", "content": JSON_HINT})
    return out


def parse_function_call(msg: dict) -> list[ToolCall]:
    fc = msg.get("function_call")
    if not fc:
        return []
    args = fc.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return [ToolCall(name=fc.get("name", ""), arguments=args, id=f"call_{uuid.uuid4().hex[:8]}")]


class GigaChatClient(LLMClient):
    name = "gigachat"

    def __init__(
        self,
        credentials: str,
        scope: str,
        model: str,
        auth_url: str,
        base_url: str,
        verify_ssl: bool = True,
        ca_bundle: str = "",
        timeout: float = 60.0,
    ):
        if not credentials:
            raise LLMError("GIGACHAT_CREDENTIALS не задан")
        self.model = model
        self._credentials = credentials
        self._scope = scope
        self._auth_url = auth_url
        verify: bool | str = ca_bundle or verify_ssl
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), verify=verify, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        r = await post_with_retry(
            self._http,
            self._auth_url,
            provider="GigaChat OAuth",
            headers={
                "Authorization": f"Basic {self._credentials}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"scope": self._scope},
        )
        if r.status_code != 200:
            raise LLMError(f"GigaChat OAuth {r.status_code}: {r.text[:300]}")
        data = r.json()
        self._token = data["access_token"]
        # expires_at приходит в миллисекундах
        self._token_expires_at = float(data.get("expires_at", 0)) / 1000 or time.time() + 1700
        return self._token

    async def models(self) -> list[str]:
        """GET /models — список доступных моделей (заодно проверка ключа и токена)."""
        token = await self._get_token()
        r = await self._http.get("/models", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if r.status_code != 200:
            raise LLMError(f"GigaChat /models {r.status_code}: {r.text[:300]}")
        return [m.get("id") for m in r.json().get("data", [])]

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
            "messages": to_gigachat_messages(messages, json_mode),
            "temperature": max(temperature, 0.01),  # GigaChat не принимает 0
            "max_tokens": max_tokens,
        }
        if tools:
            body["functions"] = to_gigachat_functions(tools)
            body["function_call"] = "auto"
        for attempt in range(2):  # 401 -> токен протух раньше срока: обновляем и повторяем
            token = await self._get_token()
            r = await post_with_retry(
                self._http, "/chat/completions", json=body,
                headers={"Authorization": f"Bearer {token}"}, provider="GigaChat",
            )
            if r.status_code != 401:
                break
            self._token = None
        if r.status_code != 200:
            raise LLMError(f"GigaChat {r.status_code}: {r.text[:300]}")
        msg = r.json()["choices"][0]["message"]
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=parse_function_call(msg),
            provider=self.name,
            model=self.model,
        )
