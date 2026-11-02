"""Отмена зависаний, очередь модели и продолжение диалога; без сети и ключей."""
import asyncio
import dataclasses
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import db, runtime
from backend.agent import actions, loop, negotiation, qa, session
from backend.llm import LLMError, LLMResponse, ToolCall
from backend.llm.base import LLMBusy
from backend.llm.gigachat import GigaChatClient, to_gigachat_messages
from backend.llm.deepseek import DeepSeekClient
from tests.fakes import ScriptedLLM, call, say


class QueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = GigaChatClient('test', 'test', 'test', 'https://example.org/oauth', 'https://example.org',
                                     max_waiters=1, queue_timeout=.04, timeout=.2)
        self.entered = asyncio.Queue()
        self.release = asyncio.Event()
        self.calls = 0

        async def generate(*a, **kw):
            self.calls += 1
            self.entered.put_nowait(self.calls)
            await self.release.wait()
            return LLMResponse('ok')
        self.client._chat = generate
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client._http.aclose()

    def start(self):
        task = asyncio.create_task(self.client.chat([]))
        self.tasks.append(task)
        return task

    async def queued(self):
        for _ in range(100):
            if self.client._waiters:
                return
            await asyncio.sleep(0)
        self.fail('Запрос не попал в очередь')

    async def test_full_queue_rejects_without_starting_generation(self):
        active = self.start()
        await self.entered.get()
        waiting = self.start()
        await self.queued()
        with self.assertRaises(LLMBusy):
            await self.client.chat([])
        self.assertEqual(self.calls, 1)
        self.release.set()
        self.assertEqual([r.text for r in await asyncio.gather(active, waiting)], ['ok', 'ok'])
        self.assertEqual(self.client._waiters, 0)

    async def test_wait_timeout_does_not_call_provider_or_leak_slot(self):
        active = self.start()
        await self.entered.get()
        with self.assertRaises(LLMBusy):
            await self.client.chat([])
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.client._waiters, 0)
        self.assertTrue(self.client._generation_lock.locked())
        self.release.set()
        await active
        self.assertEqual((await self.client.chat([])).text, 'ok')

    async def test_cancelled_waiter_frees_slot_and_clears_ui(self):
        active = self.start()
        await self.entered.get()
        signals = []
        turn = runtime.Turn('queue', 1, lambda event, **kw: signals.append((event, kw['on'])))
        waiting = asyncio.create_task(turn.run(lambda: self.client.chat([]), kind='llm', name='agent'))
        self.tasks.append(waiting)
        await self.queued()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertEqual(signals, [('model_wait', True), ('model_wait', False)])
        self.assertEqual(self.client._waiters, 0)
        next_waiter = self.start()
        await self.queued()
        self.release.set()
        await asyncio.gather(active, next_waiter)

    async def test_cancelled_generation_releases_lock_for_next_dialogue(self):
        active = self.start()
        await self.entered.get()
        waiting = self.start()
        await self.queued()
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        self.assertEqual(await asyncio.wait_for(self.entered.get(), .1), 2)
        self.release.set()
        self.assertEqual((await waiting).text, 'ok')
        self.assertFalse(self.client._generation_lock.locked())

    async def test_whole_generation_timeout_releases_lock(self):
        self.client._call_timeout = .02
        with self.assertRaises(LLMError):
            await self.client.chat([])
        self.assertFalse(self.client._generation_lock.locked())
        self.release.set()
        self.assertEqual((await self.client.chat([])).text, 'ok')


class TurnTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.configure('sqlite://')
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, s):
        queue = asyncio.Queue()
        s.subscribers.add(queue)
        task = asyncio.create_task(loop.run_session(s))
        self.tasks.append(task)
        return queue

    async def until(self, queue, predicate):
        while True:
            event = await asyncio.wait_for(queue.get(), 1)
            if predicate(event):
                return event

    async def test_human_card_wait_does_not_use_active_budget(self):
        s = session.create_session('Квартира', ScriptedLLM())
        turn = runtime.Turn(s.id, .04)
        task = asyncio.create_task(turn.run(lambda: s.ask('clarify', 'Город', 'Какой город?'),
                                            kind='tool', name='ask_client'))
        self.tasks.append(task)
        while not s.pending:
            await asyncio.sleep(0)
        await asyncio.sleep(.08)
        self.assertFalse(task.done())
        s.resolve('clarify', True, {'text': 'Казань'})
        self.assertEqual((await task)['data']['text'], 'Казань')
        self.assertLess(turn.elapsed, .04)

    async def test_seller_wait_does_not_use_active_budget(self):
        queue = asyncio.Queue()
        turn = runtime.Turn('seller', .04)
        task = asyncio.create_task(turn.run(lambda: negotiation.queue_input(queue, timeout=1),
                                            kind='tool', name='call_seller'))
        self.tasks.append(task)
        await asyncio.sleep(.08)
        self.assertFalse(task.done())
        queue.put_nowait('Согласен')
        self.assertEqual(await task, 'Согласен')

    async def test_deadline_is_shared_by_operations_and_cancels_work(self):
        turn = runtime.Turn('test', .08)
        await turn.run(lambda: asyncio.sleep(.05), kind='llm', name='agent')
        cancelled = asyncio.Event()

        async def stuck():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with self.assertRaises(runtime.TurnStopped):
            await turn.run(stuck, kind='tool', name='search_listings')
        self.assertTrue(cancelled.is_set())

    async def test_timed_out_batch_has_results_and_can_continue_with_same_state(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        batch = LLMResponse('', [ToolCall('search_listings', {}, 'a'), ToolCall('show_selection', {}, 'b')],
                            functions_state_id='preserved')
        s = session.create_session('Квартира', ScriptedLLM(batch, say('Продолжаю')))
        s.profile = {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000}
        s.red_lines_ok = True

        async def stuck(*a):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(loop, 'settings', dataclasses.replace(loop.settings, agent_turn_timeout_s=.05)), \
             patch.object(actions, 'execute', stuck):
            queue = self.start(s)
            await self.until(queue, lambda e: e.get('await_reply'))
            self.assertTrue(entered.is_set() and cancelled.is_set())
            self.assertEqual([m['tool_call_id'] for m in s.messages if m['role'] == 'tool'], ['a', 'b'])
            to_gigachat_messages(s.messages, False)  # нет потерянных/непарных результатов
            self.assertEqual(s.profile['budget_max'], 9_000_000)
            self.assertTrue(s.red_lines_ok)
        from backend import sources
        from tests.test_llm_and_agent import listing
        with patch.object(sources, 'collect', AsyncMock(return_value=([listing()], []))), patch.object(sources, 'more', AsyncMock(return_value=[])):
            s.llm.review_intent = 'continue'
            await s.receive_text('Продолжить')
            event = await self.until(queue, lambda e: e.get('kind') == 'selection' and e['type'] == 'confirm')
            self.assertEqual(s.profile['budget_max'], 9_000_000)
            self.assertTrue(s.shown)
            to_gigachat_messages(s.messages, False)

    async def test_repeated_identical_action_stops_before_third_execution(self):
        replies = [LLMResponse('', [ToolCall('update_request', {'city': 'Казань'}, f'c{i}')]) for i in range(3)]
        s = session.create_session('Квартира', ScriptedLLM(*replies, say('Продолжим')))
        queue = self.start(s)
        stopped = await self.until(queue, lambda e: e.get('await_reply'))
        self.assertIn('повтор', stopped['text'])
        self.assertEqual(len([m for m in s.messages if m['role'] == 'tool' and 'error' not in json.loads(m['content'])]), 2)
        to_gigachat_messages(s.messages, False)
        await s.receive_text('Продолжить')
        await self.until(queue, lambda e: e.get('text') == 'Продолжим')

    async def test_busy_model_keeps_confirmation_open(self):
        class Busy(ScriptedLLM):
            async def chat(self, *a, **kw):
                raise LLMBusy('busy')
        s = session.create_session('Квартира', Busy())
        card = asyncio.create_task(s.ask('clarify', 'Вопрос', 'Город?'))
        self.tasks.append(card)
        await asyncio.sleep(0)
        await qa.answer(s, 'Что такое эскроу?')
        self.assertIn('clarify', s.pending)
        self.assertFalse(card.done())
        self.assertIn('занята', s.history[-1]['text'])

    async def test_diagnostics_do_not_log_arguments_or_exception_text(self):
        secret = 'PRIVATE_CLIENT_TEXT'
        turn = runtime.Turn('test', 1)

        async def fail():
            raise LLMError(secret)
        with self.assertLogs('agent.runtime') as captured:
            with self.assertRaises(LLMError):
                await turn.run(fail, kind='tool', name='search_listings')
            turn.finish('LLMError')
        logs = '\n'.join(captured.output)
        self.assertIn('search_listings', logs)
        self.assertIn('LLMError', logs)
        self.assertIn('active_ms', logs)
        self.assertIn(turn.id, logs)
        self.assertNotIn(secret, logs)

    async def test_empty_model_reply_gives_retry_message_instead_of_silent_wait(self):
        s = session.create_session('Квартира', ScriptedLLM(say(''), say('Продолжаю')))
        queue = self.start(s)
        failed = await self.until(queue, lambda e: e.get('await_reply'))
        self.assertIn('не отвечает', failed['text'])
        await s.receive_text('Продолжить')
        await self.until(queue, lambda e: e.get('text') == 'Продолжаю')

    async def test_deepseek_deadline_cancels_transport_without_background_retries(self):
        cancelled = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            try:
                await release.wait()
            finally:
                cancelled.set()
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

        client = DeepSeekClient('test', 'https://example.org', 'test', timeout=.02)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://example.org')
        try:
            with self.assertRaises(LLMError):
                await client.chat([])
            self.assertTrue(cancelled.is_set())
            self.assertEqual(calls, 1)
            release.set()
            self.assertEqual((await client.chat([])).text, 'ok')
        finally:
            await client._http.aclose()
