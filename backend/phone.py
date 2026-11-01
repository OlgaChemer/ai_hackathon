"""Настоящий звонок продавцу через МТС Exolve.

Звонит браузер: JsSIP подключается к webrtc.exolve.ru и набирает номер, в линию
идёт голос агента, а голос продавца — обратно в браузер и на распознавание (Whisper).
Сервер здесь делает две вещи:
- синтез речи агента (Media API → WAV);
- выдаёт браузеру логин и пароль SIP ID приложения (берёт их по API).

Что нужно в личном кабинете Exolve: купить номер и создать SIP ID с этим номером
как определяемым. Ключ приложения — EXOLVE_API_KEY в .env.

Проверка настроек:  .venv/bin/python -m backend.phone
"""
from __future__ import annotations

import base64
import io
import re
import secrets
import time
import wave

import httpx

from .config import settings

API = "https://api.exolve.ru"
WEBRTC_WS = "wss://webrtc.exolve.ru:443"
WEBRTC_REALM = "webrtc.exolve.ru"
MAX_TTS = 480  # лимит Exolve — 500 символов на запрос

_creds: dict | None = None
_creds_at = 0.0


class PhoneError(RuntimeError):
    """Exolve не настроен или не ответил."""


def configured() -> bool:
    return bool(settings.exolve_api_key)


async def _post(path: str, body: dict, timeout: float = 20) -> dict:
    if not configured():
        raise PhoneError("Нет EXOLVE_API_KEY в .env")
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
            r = await client.post(f"{API}/{path}", json=body,
                                  headers={"Authorization": f"Bearer {settings.exolve_api_key}"})
    except httpx.HTTPError as exc:  # сеть до api.exolve.ru — понятная ошибка, а не 500
        raise PhoneError(f"Нет связи с Exolve ({type(exc).__name__}). Проверьте интернет и попробуйте ещё раз.") from exc
    if r.status_code != 200:
        raise PhoneError(f"Exolve {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


# ------------------------------------------------------------------ SIP-доступ для браузера

async def credentials() -> dict:
    """Логин и пароль первого SIP ID приложения (кешируются на 10 минут)."""
    global _creds, _creds_at
    if _creds and time.monotonic() - _creds_at < 600:
        return _creds
    sips = (await _post("number/customer/v1/GetSIPList", {"limit": 10, "offset": 0})).get("sips") or []
    if not sips:
        raise PhoneError("В приложении Exolve нет SIP ID. Купите номер и создайте SIP ID в личном кабинете "
                         "(раздел «SIP-подключения»), выбрав этот номер определяемым.")
    sip = next((s for s in sips if s.get("cli")), sips[0])
    attrs = (await _post("sip/v1/GetAttributes", {"sip_resource_id": sip["sip_resource_id"]})).get("attributes") or {}
    if not attrs.get("password"):
        raise PhoneError("Exolve не вернул пароль SIP ID")
    _creds = {
        "username": attrs.get("username") or attrs.get("login") or sip["user_name"],
        "password": attrs["password"],
        "cli": attrs.get("cli") or sip.get("cli") or "",
        "ws": WEBRTC_WS,
        "realm": WEBRTC_REALM,
    }
    _creds_at = time.monotonic()
    return _creds


# ------------------------------------------------------------------ синтез речи

SPEAK_FIXES = [
    (r"₽", " рублей"),
    (r"\bм²", " квадратных метров"),
    (r"\bкв\.\s?м\b", "квадратных метров"),
    (r"\bИИ-", "И И "),
    (r"\bЕГРН\b", "Е Г Р Н"),
    (r"\bСБР\b", "сервис безопасных расчётов"),
]


def speakable(text: str) -> str:
    for pattern, repl in SPEAK_FIXES:
        text = re.sub(pattern, repl, text)
    return re.sub(r"\s+", " ", text).strip()


def _chunks(text: str) -> list[str]:
    """Режет текст по предложениям на куски не длиннее лимита Exolve."""
    parts, cur = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        while len(sentence) > MAX_TTS:  # очень длинное предложение — по словам
            cut = sentence.rfind(" ", 0, MAX_TTS)
            cut = cut if cut > 0 else MAX_TTS
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        if len(cur) + len(sentence) + 1 > MAX_TTS:
            parts.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        parts.append(cur)
    return parts


def _join_wavs(blobs: list[bytes]) -> bytes:
    if len(blobs) == 1:
        return blobs[0]
    out = io.BytesIO()
    with wave.open(io.BytesIO(blobs[0])) as first:
        params = first.getparams()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for blob in blobs:
            with wave.open(io.BytesIO(blob)) as part:
                w.writeframes(part.readframes(part.getnframes()))
    return out.getvalue()


_tts_cache: dict[tuple[str, int], bytes] = {}


async def synthesize(text: str, voice: int | None = None) -> bytes:
    """Текст → WAV (голос Exolve). Повторяющиеся фразы берутся из кеша."""
    voice = voice or settings.exolve_voice
    clean = speakable(text)
    if not clean:
        raise PhoneError("Пустой текст для синтеза")
    if (clean, voice) in _tts_cache:
        return _tts_cache[(clean, voice)]
    blobs = []
    for chunk in _chunks(clean):
        data = await _post("media/v1/Synthesize", {
            "text": chunk,
            "voice_settings": {"lang": 1, "voice": voice, "emotion": 1, "speed": 1.1, "loudness_normalization": -19},
        }, timeout=30)
        if not data.get("file"):
            raise PhoneError("Exolve не вернул аудио")
        blobs.append(base64.b64decode(data["file"]))
    audio = _join_wavs(blobs)
    if len(_tts_cache) >= 64:
        _tts_cache.pop(next(iter(_tts_cache)))
    _tts_cache[(clean, voice)] = audio
    return audio


def status() -> dict:
    return {"configured": configured(), "seller_phone": settings.seller_phone, "pin": bool(settings.phone_pin)}


def allowed(pin: str | None) -> bool:
    """Звонок и синтез — только с PIN, если он задан (PHONE_PIN в .env)."""
    return not settings.phone_pin or secrets.compare_digest(pin or "", settings.phone_pin)


if __name__ == "__main__":
    import asyncio

    async def check():
        print("Ключ:", "есть" if configured() else "нет EXOLVE_API_KEY")
        audio = await synthesize("Здравствуйте! Проверка синтеза речи.")
        print(f"Синтез речи: ок, {len(audio)} байт WAV")
        try:
            c = await credentials()
            print(f"SIP ID: {c['username']}, определяемый номер: {c['cli'] or 'не задан'}")
        except PhoneError as exc:
            print("SIP ID:", exc)

    asyncio.run(check())
