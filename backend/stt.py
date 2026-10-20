"""Распознавание речи на сервере: локальный Whisper (faster-whisper), без облаков.

Браузер записывает фразу (webm/opus) и присылает её на POST /api/stt.
Модель грузится один раз в фоне при старте — только если уже скачана
(сервер сам ничего не качает, чтобы не зависнуть на медленной сети):
    .venv/bin/hf download Systran/faster-whisper-small

Настройки (.env): WHISPER_MODEL=small|base|medium, WHISPER_DEVICE=cpu|cuda,
WHISPER_COMPUTE=int8|float16. В проде на это место встанет SaluteSpeech.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")

# Подсказка модели: слова, которые чаще всего звучат в запросах (Whisper лучше их узнаёт)
VOCABULARY = ("Ищу квартиру в Москве, Санкт-Петербурге, Казани, Новосибирске, Екатеринбурге. "
              "Однокомнатная, двухкомнатная, трёхкомнатная, студия. Бюджет до 9 миллионов рублей, ипотека, "
              "первый взнос, рядом школа, метро, район, юго-запад, кухня, этаж.")

_model = None
_state = {"status": "not_loaded", "error": None, "model": MODEL_NAME}
_lock = threading.Lock()


def installed() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def status() -> dict:
    return {**_state, "installed": installed()}


def _load():
    global _model
    with _lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        _state.update(status="loading", error=None)
        t0 = time.time()
        try:
            _model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE, local_files_only=True)
        except Exception as exc:  # модель не скачана, нет памяти и т.п.
            hint = f"Модель не скачана: .venv/bin/hf download Systran/faster-whisper-{MODEL_NAME}" \
                if "local" in str(exc).lower() or "snapshot" in str(exc).lower() or "find" in str(exc).lower() else str(exc)[:300]
            _state.update(status="error", error=hint)
            log.warning("Whisper не загрузился: %s", hint)
            raise
        _state.update(status="ready")
        log.info("Whisper %s загружен за %.1f с", MODEL_NAME, time.time() - t0)
        return _model


def preload_in_background() -> None:
    """Грузим модель заранее, чтобы первая фраза не ждала загрузки."""
    if installed() and _model is None:
        threading.Thread(target=lambda: _safe_load(), daemon=True, name="whisper-preload").start()


def _safe_load():
    try:
        _load()
    except Exception:
        pass


def decode(audio: bytes, rate: int = 16000):
    """webm/ogg/wav -> float32 моно 16 кГц. Свой декодер: встроенный в faster-whisper
    передаёт в PyAV параметр metadata_errors, которого нет в PyAV 15+."""
    import av
    import numpy as np

    chunks = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # хвост ресемплера
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _transcribe_sync(audio: bytes) -> dict:
    model = _load()
    t0 = time.time()
    samples = decode(audio)
    if samples.size < 1600:  # меньше 0,1 с
        return {"text": "", "seconds": 0.0, "duration": round(samples.size / 16000, 2)}
    segments, info = model.transcribe(
        samples,
        language="ru",
        beam_size=1,
        vad_filter=True,               # отрезаем тишину до и после фразы
        condition_on_previous_text=False,
        initial_prompt=VOCABULARY,     # словарь темы: города, «комнатная», «ипотека»
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return {"text": text, "seconds": round(time.time() - t0, 2), "duration": round(info.duration, 2)}


async def transcribe(audio: bytes) -> dict:
    return await asyncio.to_thread(_transcribe_sync, audio)


def download(model: str = MODEL_NAME) -> str:
    """Скачать модель заранее (при установке), чтобы сервер не качал её при первой фразе."""
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # новый протокол загрузки Hugging Face у нас зависал
    from huggingface_hub import snapshot_download

    repo = model if "/" in model else f"Systran/faster-whisper-{model}"
    return snapshot_download(repo_id=repo)


if __name__ == "__main__":
    # .venv/bin/python -m backend.stt download [small|base|medium]
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "download":
        name = sys.argv[2] if len(sys.argv) > 2 else MODEL_NAME
        print(f"Скачиваю Whisper «{name}» (~460 МБ для small)…")
        path = download(name)
        print(f"Готово: {path}")
        _load()
        print("Модель загружается — распознавание на сервере готово.")
    else:
        print(__doc__)
