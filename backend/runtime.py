"""Небольшой бюджет активной работы и диагностика без текстов/аргументов клиента."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import time
import uuid

log = logging.getLogger("agent.runtime")
_current: ContextVar[Turn | None] = ContextVar("agent_turn", default=None)


class TurnStopped(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def record(event: str, **fields) -> None:
    turn = _current.get()
    context = {"session": turn.session_id, "turn": turn.id} if turn else {}
    log.info("%s", json.dumps({"event": event, **context, **fields}, ensure_ascii=False))


@contextmanager
def timed(event: str, **fields):
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        record(event, **fields, duration_ms=round((time.monotonic() - started) * 1000), outcome=outcome)


def model_wait(on: bool) -> None:
    turn = _current.get()
    if turn and turn.notify:
        turn.notify("model_wait", on=on)


@contextmanager
def human_wait():
    """Ожидание человека не расходует вычислительное время хода."""
    turn = _current.get()
    if turn is None:
        yield
        return
    turn.pause_depth += 1
    if turn.pause_depth == 1:
        turn.spent += time.monotonic() - turn.active_since
        turn.changed.set()
    try:
        yield
    finally:
        turn.pause_depth -= 1
        if not turn.pause_depth:
            turn.active_since = time.monotonic()
            turn.changed.set()


class Turn:
    def __init__(self, session_id: str, seconds: float, notify=None):
        self.id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.seconds = seconds
        self.notify = notify
        self.started = self.active_since = time.monotonic()
        self.spent = 0.0
        self.pause_depth = 0
        self.changed = asyncio.Event()
        self.llm_calls = self.tool_calls = 0
        self.last_call = None
        self.repeats = 0

    @property
    def elapsed(self) -> float:
        return self.spent + (0 if self.pause_depth else time.monotonic() - self.active_since)

    def check_repeat(self, name: str, arguments: dict) -> None:
        # Сохраняем только отпечаток; аргументы никогда не попадают в технический лог.
        key = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, default=str).encode()).digest()
        self.repeats = self.repeats + 1 if key == self.last_call else 1
        self.last_call = key
        if self.repeats >= 3:
            raise TurnStopped("repeated_tool")

    async def _deadline(self):
        while True:
            self.changed.clear()
            remaining = self.seconds - self.elapsed
            if remaining <= 0:
                return
            if self.pause_depth:
                await self.changed.wait()
            else:
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return

    async def run(self, operation, *, kind: str, name: str):
        """Отменяет зависшую операцию; перед выходом дожидается её очистки."""
        if self.elapsed >= self.seconds:
            raise TurnStopped("timeout")
        if kind == "llm":
            self.llm_calls += 1
        else:
            self.tool_calls += 1
        token = _current.set(self)
        started = self.elapsed
        wall_started = time.monotonic()
        outcome = "ok"
        task = asyncio.create_task(operation())
        timer = asyncio.create_task(self._deadline())
        try:
            done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                result = task.result()
                if kind == "tool" and isinstance(result, dict) and result.get("error"):
                    outcome = "rejected"
                return result
            raise TurnStopped("timeout")
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            task.cancel()
            timer.cancel()
            await asyncio.gather(task, timer, return_exceptions=True)
            record("operation", kind=kind, name=name, outcome=outcome,
                   duration_ms=round((time.monotonic() - wall_started) * 1000),
                   active_ms=round((self.elapsed - started) * 1000))
            _current.reset(token)

    def finish(self, outcome: str) -> None:
        token = _current.set(self)
        try:
            record("turn", outcome=outcome, duration_ms=round((time.monotonic() - self.started) * 1000),
                   active_ms=round(self.elapsed * 1000), llm_operations=self.llm_calls, tool_calls=self.tool_calls)
        finally:
            _current.reset(token)
