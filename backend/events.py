"""Очередь SSE: промежуточные сигналы объединяются, факты восстанавливаются из БД."""
import asyncio
from collections import deque
from . import telemetry

TRANSIENT=frozenset(('typing','model_wait','search_progress'))


class EventBuffer:
    def __init__(self,capacity):
        self.maxsize=capacity;self.items=deque();self.ready=asyncio.Event();self.closed=False

    def qsize(self):return len(self.items)
    def empty(self):return not self.items

    def put_nowait(self,event):
        if self.closed:return
        if event['type'] in TRANSIENT:
            before=len(self.items)
            self.items=deque(e for e in self.items if e['type']!=event['type'])
            if len(self.items)<before:telemetry.count('sse_coalesced',before-len(self.items))
        if len(self.items)>=self.maxsize:raise asyncio.QueueFull()
        self.items.append(event);self.ready.set()

    def get_nowait(self):
        if not self.items:raise asyncio.QueueEmpty()
        value=self.items.popleft()
        if not self.items:self.ready.clear()
        return value

    async def get(self):
        while not self.items:await self.ready.wait()
        return self.get_nowait()

    def resync(self):
        self.items.clear();self.items.append({'type':'resync_required','seq':0})
        self.closed=True;self.ready.set()


def deliver(session,event):
    for queue in list(session.subscribers):
        try:queue.put_nowait(event)
        except asyncio.QueueFull:
            session.subscribers.discard(queue)
            if hasattr(queue,'resync'):queue.resync()
            else:
                while not queue.empty():queue.get_nowait()
                queue.put_nowait({'type':'resync_required','seq':0})
            telemetry.count('sse_resync')
