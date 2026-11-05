"""HTTP-слой: API агента + раздача фронтенда.

Запуск:  .venv/bin/uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import fields, asdict

import httpx

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from . import auth, db, economics, sources
from .sources import places
from .agent import session as O
from .agent.loop import run_session
from .config import ROOT, settings
from .db import repo
from .llm import LLMError, get_llm
from . import maptiles, phone, ratelimit, stt
from .agent import guard
from . import telemetry,readiness,phone_access
from .events import EventBuffer

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Агент жизненных событий — Домклик")
app.middleware("http")(auth.visitor_middleware)


@app.exception_handler(db.PersistenceError)
async def persistence_failure(request: Request, exc: db.PersistenceError):
    return JSONResponse({'detail':str(exc),'error_kind':'revision_conflict' if isinstance(exc,db.StateConflict) else 'persistence'},
                        status_code=409 if isinstance(exc,db.StateConflict) else 503)


@app.exception_handler(db.CommandCapacityError)
async def command_capacity(request:Request,exc:db.CommandCapacityError):
    telemetry.count('command_rejected')
    return JSONResponse({'detail':str(exc),'error_kind':'capacity'},status_code=429,headers={'Retry-After':'2'})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def _limits_and_headers(request: Request, call_next):
    uid = auth.whoami(request)[1] if request.cookies.get(auth.SESSION_COOKIE) else None
    wait = ratelimit.check(request.method, request.url.path,
                           request.client.host if request.client else "unknown", uid)
    if wait is not None:
        response = JSONResponse({"detail": "Слишком много запросов. Попробуйте немного позже."}, status_code=429,
                                headers={"Retry-After": str(math.ceil(wait))})
    else:
        response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _cooldown(s):
    if s is not None and s.blocked_recently() >= 5:
        raise HTTPException(429, "Слишком много отклонённых сообщений. Сделаем паузу на пару минут.",
                            headers={"Retry-After": "120"})


@app.on_event("startup")
async def _startup():
    db.configure()
    stt.preload_in_background()
    asyncio.create_task(_reaper())
    if settings.refresh_at:  # ночной обход объявлений внутри сервера (или отдельно: python -m backend.workers.refresh --once)
        from .workers import refresh
        asyncio.create_task(refresh.daily(settings.refresh_at))


async def _reaper():
    """Раз в 5 минут освобождаем память от брошенных диалогов (история остаётся в базе)."""
    while True:
        await asyncio.sleep(300)
        O.reap_idle()


class StartRequest(BaseModel):
    voice_mode: StrictBool = False
    query: str = Field(min_length=3, max_length=500)


class ConfirmRequest(BaseModel):
    command_id: str | None = Field(default=None,min_length=1,max_length=64)
    voice_mode: StrictBool = False
    kind: str = Field(max_length=40)
    approved: StrictBool = False
    decision_id: str | None = Field(default=None, max_length=40)
    data: dict = {}


class SellerReply(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ClientMessage(BaseModel):
    command_id: str | None = Field(default=None,min_length=1,max_length=64)
    voice_mode: StrictBool = False
    text: str = Field(min_length=1, max_length=1000)
    liked: list[str] = []
    disliked: list[str] = []


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class PhoneRequest(BaseModel):
    chat_id: str=Field(max_length=32)
    decision_id: str=Field(max_length=40)
    destination: str=Field(pattern=r'^7\d{10}$')


class AuthRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=120)


class ProductRequest(BaseModel):
    product: str
    amount: int = Field(ge=0)


@app.get("/api/health")
async def health():
    try:
        llm, reason = get_llm(), None
    except LLMError as exc:  # модель не настроена — агент не работает, интерфейс это покажет
        llm, reason = None, str(exc)
    return {
        "ok": llm is not None,
        "provider": llm.name if llm else settings.llm_provider,
        "model": llm.model if llm else None,
        "configured": settings.llm_provider,
        "reason": reason,
        "cities": sources.SUPPORTED_CITIES,
        "featured_cities": sources.FEATURED_CITIES,
        "stt": stt.status(),
        "places": places.status(),
        "phone": phone.status(),
    }


@app.get('/api/health/live')
async def liveness():return {'live':True}


@app.get('/api/health/ready')
async def ready():
    try:llm=get_llm()
    except LLMError:llm=None
    result=readiness.report(llm)
    result['capabilities']={'speech':stt.status(),'infrastructure':places.status(),'phone':phone.status()}
    return JSONResponse(result,status_code=200 if result['ready'] else 503)


@app.get('/api/metrics')
async def metrics():
    result=telemetry.snapshot();sessions=list(O.SESSIONS.values())
    subscribers=[q for s in sessions for q in s.subscribers]
    result['queues']={'commands':sum(s.command_queue.qsize() for s in sessions),
                      'questions':sum(len(s.qa_pending) for s in sessions),
                      'subscribers':len(subscribers),'sse_backlog':sum(q.qsize() for q in subscribers),
                      'active_dialogues':sum(O.running(s) for s in sessions)}
    return result


MAX_AUDIO = 5 * 1024 * 1024  # ~ минута речи в webm/opus


@app.get("/api/map")
async def map_config():
    return maptiles.configuration()


@app.get("/api/map/tiles/{z}/{x}/{y}.png")
async def map_tile(z: int, x: int, y: int):
    try:
        image = await maptiles.tile(z, x, y)
    except ValueError:
        raise HTTPException(404, "Нет такого фрагмента карты") from None
    except maptiles.TileUnavailable:
        raise HTTPException(503, "Подложка 2ГИС временно недоступна", headers={"Cache-Control": "no-store"}) from None
    return Response(image, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


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


def _check_pin(request: Request):
    if not phone.allowed(request.headers.get("X-Phone-Pin")):
        raise HTTPException(403, "Нужен PIN для звонков (PHONE_PIN в .env сервера)")


@app.post("/api/phone")
async def phone_config(req:PhoneRequest,request: Request):
    """SIP-доступ Exolve для звонка из браузера. На публичном сервере — только с PIN."""
    _check_pin(request)
    s=_session(req.chat_id,request)
    who=phone_access.actor(*auth.whoami(request))
    grant=db.required(phone_access.reserve,s,who,req.decision_id)
    if grant.get('error'):raise HTTPException(grant['status'],grant['error'])
    try:
        return {**await phone.credentials(),'grant_token':grant['token'],'destination':req.destination,
                'listing_id':grant['listing_id'],'max_seconds':settings.phone_call_max_seconds}
    except phone.PhoneError as exc:
        db.required(phone_access.close,grant['token'],who)
        raise HTTPException(503, str(exc)) from exc


@app.post('/api/phone/close')
async def phone_close(request:Request):
    who=phone_access.actor(*auth.whoami(request))
    return {'closed':db.required(phone_access.close,request.headers.get('X-Phone-Grant'),who)}


@app.post("/api/tts")
async def text_to_speech(req: SpeakRequest, request: Request):
    """Голос агента для телефонного звонка: текст -> WAV."""
    _check_pin(request)
    who=phone_access.actor(*auth.whoami(request))
    if not db.required(phone_access.charge_tts,request.headers.get('X-Phone-Grant'),who,len(req.text)):
        raise HTTPException(403,'Разрешение на синтез истекло, не относится к этому диалогу или лимит речи исчерпан.')
    try:
        audio = await phone.synthesize(req.text)
    except (phone.PhoneError, httpx.HTTPError) as exc:
        raise HTTPException(502, f"Синтез речи не удался: {exc}") from exc
    return Response(audio, media_type="audio/wav")


# ------------------------------------------------------------------ вход


def _claim_live(request: Request, user: dict) -> None:
    """Вход: живые диалоги этого браузера тоже переходят пользователю (в базе — repo.claim_visitor)."""
    vid = auth.visitor_id(request)
    for s in O.SESSIONS.values():
        if vid and s.visitor_id == vid and s.user_id is None:
            s.user_id = user["id"]


@app.post("/api/auth/register")
async def register(req: AuthRequest, request: Request, response: Response):
    r = auth.register(req.email, req.password, req.name, request, response)
    _claim_live(request, r["user"])
    return r


@app.post("/api/auth/login")
async def login(req: AuthRequest, request: Request, response: Response):
    r = auth.login(req.email, req.password, request, response)
    _claim_live(request, r["user"])
    return r


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return {"user": auth.current_user(request)}


# ------------------------------------------------------------------ диалоги


@app.post("/api/session")
async def start(req: StartRequest, request: Request):
    if sum(O.running(s) for s in O.SESSIONS.values())>=settings.max_active_dialogues:
        raise HTTPException(503,'Сейчас обрабатывается много диалогов. Повторите позже.',headers={'Retry-After':'5'})
    vid, uid = auth.whoami(request)
    s = O.create_session(req.query, _llm(), visitor_id=vid, user_id=uid)
    s.voice_mode = req.voice_mode
    return {"id": s.id}


def _llm():
    try:
        return get_llm()
    except LLMError as exc:
        raise HTTPException(503, f"Модель не настроена: {exc}") from exc


def _owns(s: O.Session, request: Request) -> bool:
    vid, uid = auth.whoami(request)
    return bool((vid and s.visitor_id == vid) or (uid is not None and s.user_id == uid))


def _session(sid: str, request: Request) -> O.Session:
    """Живая сессия агента — только своему браузеру или пользователю; чужой id — как несуществующий."""
    s = O.SESSIONS.get(sid)
    if s is None and _can_open(sid,request):s=O.restore_session(sid,'',_llm())
    if s is None or not _owns(s, request):
        raise HTTPException(404, "Диалог не найден")
    return s


def _can_open(sid: str, request: Request) -> bool:
    live = O.SESSIONS.get(sid)
    if live is not None:
        return _owns(live, request)
    vid, uid = auth.whoami(request)
    return bool(db.safe(lambda d: repo.can_open(repo.get_chat(d, sid), vid, uid), default=False))


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(guard.output(ev, limit=False), ensure_ascii=False)}\n\n"


@app.get("/api/chats")
async def chats(request: Request):
    """Диалоги этого браузера и пользователя — новые сверху."""
    vid, uid = auth.whoami(request)
    items = db.safe(repo.list_chats, vid, uid, default=[]) or []
    for c in items:
        c["live"] = O.running(O.SESSIONS.get(c["id"]))
    return {"chats": items}


@app.get("/api/chat/{sid}/stream")
async def chat_stream(sid: str, request: Request, after: int = 0):
    """Лента диалога: история после события after (replay: true), затем replay_end, затем живые события.
    Новый диалог здесь же и запускается. Закрытая вкладка агента не останавливает."""
    if not _can_open(sid, request):
        raise HTTPException(404, "Диалог не найден")
    live = O.SESSIONS.get(sid)
    if live is None:
        info=db.required(repo.get_chat,sid)
        if info and info.state:live=O.restore_session(sid,'',_llm())
    if live is not None:
        if live.task is None and sum(O.running(s) for s in O.SESSIONS.values())>=settings.max_active_dialogues:
            raise HTTPException(503,'Все обработчики диалогов заняты. История сохранена; повторите позже.',headers={'Retry-After':'5'})
        if len(live.subscribers)>=settings.max_dialogue_subscribers:
            raise HTTPException(429,'Этот диалог уже открыт в нескольких вкладках.',headers={'Retry-After':'5'})

    async def gen():
        q = EventBuffer(settings.sse_queue_capacity)
        if live is not None:
            if ((live.task is None and sum(O.running(s) for s in O.SESSIONS.values())>=settings.max_active_dialogues)
                    or len(live.subscribers)>=settings.max_dialogue_subscribers):
                yield _sse({'type':'error','seq':0,'error_kind':'capacity','text':'Обработчики заняты. Диалог сохранён; повторите позже.'})
                return
            live.subscribers.add(q)  # сначала подписка, потом история — ни одно событие не потеряется
            if live.task is None:  # новый диалог (или продолжение) запускаем, когда вкладка уже слушает
                live.task = asyncio.create_task(run_session(live))
        try:
            past = db.safe(repo.chat_events, sid, after)
            if past is None and live is not None:  # база недоступна — из памяти этого запуска
                past = [e for e in live.history if e["seq"] > after]
            sent = after
            for ev in past or []:
                yield _sse({**ev, "replay": True})
                sent = max(sent, ev.get("seq", sent))
            running = live is not None and live.task is not None and not live.task.done()
            yield _sse({"type": "replay_end", "live": running, "seq": sent})
            if not running:
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected() or live.task.done():
                        break
                    yield ": ping\n\n"
                    continue
                if ev["seq"]:  # сигналы («агент думает») без номера — не в истории, идут как есть
                    if ev["seq"] <= sent:
                        continue
                    sent = ev["seq"]
                yield _sse(ev)
                if ev['type']=='resync_required':break
                if ev["type"] == "error":
                    break
        finally:
            if live is not None:
                live.subscribers.discard(q)
                if not live.subscribers:
                    live.idle_since = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/{sid}/message")
async def chat_message(sid: str, req: ClientMessage, request: Request):
    """Сообщение в диалог. Агент работает — получит его; остановлен (перезапуск сервера, долгая пауза) —
    поднимаем агента с памятью диалога. continued — вкладке нужно переоткрыть ленту с события after."""
    if not _can_open(sid, request):
        raise HTTPException(404, "Диалог не найден")
    live = O.SESSIONS.get(sid)
    _cooldown(live)
    if live is not None and (live.task is None or O.running(live)):  # агент ещё не запущен или работает
        live.voice_mode = req.voice_mode
        kind = await live.receive_text(req.text.strip(), {"liked": req.liked, "disliked": req.disliked},req.command_id)
        return {"ok": True, "handled_as": kind, "continued": False}
    s = O.restore_session(sid, '', _llm())
    s.voice_mode = req.voice_mode
    after=s.seq
    kind=await s.receive_text(req.text.strip(),{'liked':req.liked,'disliked':req.disliked},req.command_id)
    return {"ok": True, "handled_as": kind, "continued": True, "after": after}


@app.post("/api/session/{sid}/confirm")
async def confirm(sid: str, req: ConfirmRequest, request: Request):
    s = _session(sid, request)
    _cooldown(s)
    try:
        s.voice_mode = req.voice_mode
        if req.kind=='call' and req.data.get('mode')=='phone':
            data=dict(req.data);token=data.pop('phone_grant_token',None)
            identifier=db.required(phone_access.identify,token,phone_access.actor(*auth.whoami(request)),sid,req.decision_id)
            if not identifier:raise HTTPException(403,'Нет разрешения на этот телефонный звонок.')
            data['phone_grant_id']=identifier;req.data=data
        resolved = await s.receive_decision(req.kind, req.approved, req.data, req.decision_id,req.command_id)
    except guard.InputRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    if not resolved:
        raise HTTPException(409, "Нет ожидающего подтверждения")
    return {"ok": True}


@app.post("/api/session/{sid}/seller")
async def seller(sid: str, req: SellerReply, request: Request):
    s = _session(sid, request)
    _cooldown(s)
    category = s.refuse(req.text, where="seller")
    if category:
        return {"ok": True, "blocked": category}
    await s.seller_queue.put(req.text.strip())
    return {"ok": True}


@app.post("/api/session/{sid}/product")
async def open_product(sid: str, req: ProductRequest, request: Request):
    """Открыть продукт из финансового плана (демо: без реального открытия)."""
    await _session(sid, request).open_product(req.product, req.amount)
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
    return {"unit": economics.unit(inputs), "scenarios": economics.scenarios(base), "defaults": asdict(economics.Inputs())}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
