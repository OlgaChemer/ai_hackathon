"""GigaChat: OAuth-токен + Chat Completions.

Отличия от OpenAI-формата, которые закрывает этот модуль:
- токен получаем по ключу авторизации (Basic) на отдельном адресе и обновляем
  за минуту до истечения (живёт ~30 минут);
- инструменты передаются как `functions`, а вызов приходит в `function_call`
  (arguments — уже объект, а не строка);
- структурированный вывод: response_format с JSON Schema и strict=true;
- сертификат НУЦ Минцифры включён в проект; альтернативный путь — GIGACHAT_CA_BUNDLE.
"""
from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
import time
import uuid

import httpx

from .base import LLMBusy, LLMClient, LLMError, LLMResponse, Tool, ToolCall, post_with_retry
from .. import runtime

JSON_HINT = "Ответь строго одним JSON-объектом без пояснений и без markdown."


def to_gigachat_functions(tools: list[Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]


def to_gigachat_messages(messages: list[dict], json_mode: bool) -> list[dict]:
    """Формат OpenAI -> GigaChat: вызов действия — function_call у ассистента, результат — сообщение role=function."""
    out = []
    consumed = set()
    for index, m in enumerate(messages):
        if index in consumed:
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for call in m["tool_calls"]:
                args = call["function"]["arguments"]
                entry = {"role": "assistant", "content": m.get("content") or "",
                         "function_call": {"name": call["function"]["name"],
                                           "arguments": json.loads(args) if isinstance(args, str) else args}}
                # Совместимость с историей раннего исправления на сервере.
                state = m.get("functions_state_id") or m.get("provider_state")
                if state:
                    entry["functions_state_id"] = state
                out.append(entry)
                # Старые диалоги DeepSeek могут содержать несколько действий за один ход.
                # Для GigaChat-2 превращаем их в последовательные пары вызов/результат.
                for j in range(index + 1, len(messages)):
                    result = messages[j]
                    if result.get("role") != "tool":
                        break
                    if result.get("tool_call_id") == call["id"]:
                        out.append({"role": "function", "name": call["function"]["name"], "content": result["content"]})
                        consumed.add(j)
                        break
        elif m.get("role") == "tool":
            raise LLMError("Результат функции без соответствующего вызова")
        else:
            out.append({k: v for k, v in m.items() if k in ("role", "content", "functions_state_id", "attachments") and v is not None})
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

    async def analyze_images(self,prompt,images):
        identifiers=[]
        try:
            for index,image in enumerate(images):
                for attempt in range(2):
                    token=await self._get_token()
                    response=await self._http.post('/files',headers={'Authorization':'Bearer '+token},
                        files={'file':(f'photo-{index}.jpg',image.data,image.mime_type)},data={'purpose':'general'})
                    if response.status_code!=401:break
                    self._token=None
                if response.status_code!=200:raise LLMError(f'Не удалось передать изображение модели: HTTP {response.status_code}')
                identifiers.append(response.json()['id'])
            messages=[{'role':'system','content':'Анализируй только видимое на фотографиях. '+prompt},
                      *[{'role':'user','content':f'Фотография {i+1}.','attachments':[identifier]} for i,identifier in enumerate(identifiers)]]
            return await self.chat(messages,json_mode=True,max_tokens=1600,temperature=.1,tag='workshop_vision')
        finally:
            if identifiers:
                async def remove(identifier):
                    try:
                        token=await self._get_token()
                        await self._http.post('/files/'+identifier+'/delete',headers={'Authorization':'Bearer '+token},timeout=4)
                    except Exception:runtime.record('vision_file_cleanup',outcome='failed')
                await asyncio.gather(*(remove(identifier) for identifier in identifiers),return_exceptions=True)

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
        max_waiters: int = 6,
        queue_timeout: float = 25.0,
    ):
        if not credentials:
            raise LLMError("GIGACHAT_CREDENTIALS не задан")
        self.model = model
        self._credentials = credentials.strip().removeprefix("Basic ").strip()
        self._scope = scope
        self._auth_url = auth_url
        verify = verify_ssl
        if verify_ssl:
            context = ssl.create_default_context()
            bundled = Path(__file__).resolve().parents[1] / "certs" / "russian_trusted_root_ca_pem.crt"
            path = Path(ca_bundle) if ca_bundle else bundled
            if ca_bundle and not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            if ca_bundle or path.exists():
                context.load_verify_locations(cafile=str(path))
            verify = context
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), verify=verify, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # В текущем тарифе физлица доступен один запрос генерации одновременно.
        # get_llm() общий для диалогов, Q&A и переговоров: они ждут очередь, а не получают 429.
        self._generation_lock = asyncio.Lock()
        self._waiters = 0
        self._max_waiters = max_waiters
        self._queue_timeout = queue_timeout
        self._call_timeout = timeout

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

    async def chat(self, messages, **kwargs) -> LLMResponse:
        # Проверка/резервирование места без await: одна event loop не переполнит очередь.
        queued = self._generation_lock.locked() or self._waiters > 0
        if queued and self._waiters >= self._max_waiters:
            runtime.record("llm_queue", outcome="full", waiting=self._waiters)
            raise LLMBusy("Очередь модели заполнена")
        started = time.monotonic()
        if queued:
            self._waiters += 1
            try:
                runtime.model_wait(True)
                try:
                    await asyncio.wait_for(self._generation_lock.acquire(), self._queue_timeout)
                except asyncio.TimeoutError:
                    runtime.record("llm_queue", outcome="timeout", waiting=self._waiters - 1)
                    raise LLMBusy("Истекло время ожидания модели") from None
            finally:
                self._waiters -= 1
                runtime.model_wait(False)
        else:
            # Для свободного Lock acquire завершается без переключения задачи.
            await self._generation_lock.acquire()
        try:
            with runtime.timed("llm", provider=self.name, model=self.model, tag=kwargs.get("tag", ""),
                               queue_ms=round((time.monotonic() - started) * 1000)):
                try:
                    return await asyncio.wait_for(self._chat(messages, **kwargs), self._call_timeout)
                except asyncio.TimeoutError:
                    raise LLMError("Истекло время ответа GigaChat") from None
        finally:
            self._generation_lock.release()

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
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": to_gigachat_messages(messages, json_mode and json_schema is None),
            "temperature": max(temperature, 0.01),  # GigaChat не принимает 0
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            body['response_format'] = {'type':'json_schema', 'schema':json_schema, 'strict':True}
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
        data = r.json()
        msg = data["choices"][0]["message"]
        resolved_model = data.get('model') or self.model
        runtime.record('llm_model', requested=self.model, resolved=resolved_model)
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=parse_function_call(msg),
            provider=self.name,
            model=resolved_model,
            functions_state_id=msg.get("functions_state_id"),
        )
