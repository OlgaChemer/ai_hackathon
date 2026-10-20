"""HTTP-слой: API агента + раздача фронтенда.

Запуск:  .venv/bin/uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import fields

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import economics, sources
from .agent import orchestrator as O
from .config import ROOT, settings
from .llm import get_llm
from . import stt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Агент жизненных событий — Домклик")


@app.on_event("startup")
async def _startup():
    stt.preload_in_background()


class StartRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)


class ConfirmRequest(BaseModel):
    kind: str
    approved: bool = True
    data: dict = {}


class SellerReply(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ClientMessage(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    liked: list[str] = []
    disliked: list[str] = []


class ProductRequest(BaseModel):
    product: str
    amount: int = Field(ge=0)


@app.get("/api/health")
async def health():
    llm = get_llm()
    return {
        "ok": True,
        "provider": llm.name,
        "model": llm.model,
        "configured": settings.llm_provider,
        "reason": getattr(llm, "reason", None),
        "cities": sources.SUPPORTED_CITIES,
        "stt": stt.status(),
    }


MAX_AUDIO = 5 * 1024 * 1024  # ~ минута речи в webm/opus


@app.post("/api/stt")
async def speech_to_text(request: Request):
    """Распознать фразу: тело запроса — аудио (webm/ogg/wav) из MediaRecorder."""
    if not stt.installed():
        raise HTTPException(503, "Серверное распознавание не установлено (pip install faster-whisper)")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Пустая запись")
    if len(audio) > MAX_AUDIO:
        raise HTTPException(413, "Слишком длинная запись")
    try:
        return await stt.transcribe(audio)
    except Exception as exc:
        raise HTTPException(500, f"Не удалось распознать: {exc}") from exc


@app.post("/api/session")
async def start(req: StartRequest):
    s = O.create_session(req.query, get_llm())
    return {"id": s.id}


def _session(sid: str) -> O.Session:
    s = O.SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Сессия не найдена")
    return s


@app.get("/api/session/{sid}/stream")
async def stream(sid: str, request: Request):
    s = _session(sid)
    if s.task is not None:
        raise HTTPException(409, "Сессия уже запущена")
    s.task = asyncio.create_task(O.run_session(s))

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(s.events.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev["type"] in {"done", "error"}:
                    break
        finally:
            if not s.task.done():
                s.task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/session/{sid}/confirm")
async def confirm(sid: str, req: ConfirmRequest):
    if not _session(sid).resolve(req.kind, req.approved, req.data):
        raise HTTPException(409, f"Нет ожидающего подтверждения «{req.kind}»")
    return {"ok": True}


@app.post("/api/session/{sid}/seller")
async def seller(sid: str, req: SellerReply):
    await _session(sid).seller_queue.put(req.text.strip())
    return {"ok": True}


@app.post("/api/session/{sid}/message")
async def message(sid: str, req: ClientMessage):
    """Текст или голос клиента: ответ на вопрос, пожелание к подборке, заметка."""
    kind = await _session(sid).receive_text(req.text.strip(), {"liked": req.liked, "disliked": req.disliked})
    return {"ok": True, "handled_as": kind}


@app.post("/api/session/{sid}/product")
async def open_product(sid: str, req: ProductRequest):
    """Открыть продукт из финансового плана (демо: без реального открытия)."""
    await _session(sid).open_product(req.product, req.amount)
    return {"ok": True}


@app.get("/api/economics")
async def get_economics(request: Request):
    """Юнит-экономика и сценарии ROI. Любое поле Inputs можно передать в query."""
    params = {}
    types = {f.name: f.type for f in fields(economics.Inputs)}
    for key, value in request.query_params.items():
        if key not in types:
            continue
        kind = str(types[key])
        try:
            if kind == "bool":
                params[key] = value.lower() in {"1", "true", "yes", "on"}
            elif kind == "float":
                params[key] = float(value)
            else:
                params[key] = int(float(value))
        except ValueError:
            raise HTTPException(422, f"Неверное значение {key}")
    inputs = economics.Inputs(**params)
    base = economics.Inputs(fin_enabled=inputs.fin_enabled)
    return {"unit": economics.unit(inputs), "scenarios": economics.scenarios(base)}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
