"""Состояние функций GigaChat и доступность действий вместо повторных карточек."""
import asyncio
import unittest

from backend import db
from backend.agent import actions, session
from backend.llm import LLMResponse, ToolCall
from backend.llm.gigachat import to_gigachat_messages
from tests.fakes import ScriptedLLM, call, drive, say

STATE = '77d3fb14-457a-46ba-937e-8d856156d003'


class GigaChatStateTest(unittest.TestCase):
    def setUp(self): db.configure('sqlite://')

    def test_function_state_survives_memory_restore_and_adapter(self):
        async def go():
            reply = LLMResponse('', [ToolCall('update_request', {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000}, 'c1')],
                                functions_state_id=STATE)
            s = session.create_session('Казань, двушка до 9 млн', ScriptedLLM(reply, say('Записал')))
            await drive(s)
            return session.restore_session(s.id, 'Продолжим', ScriptedLLM())
        restored = asyncio.run(go())
        converted = to_gigachat_messages(restored.messages, False)
        fc = next(m for m in converted if m.get('function_call'))
        self.assertEqual(fc['functions_state_id'], STATE)
        self.assertEqual(converted[converted.index(fc) + 1]['role'], 'function')

    def test_state_on_text_only_reply_is_forwarded(self):
        result = to_gigachat_messages([{'role': 'assistant', 'content': 'Ответ', 'functions_state_id': STATE}], False)
        self.assertEqual(result[0]['functions_state_id'], STATE)

    def test_old_deepseek_parallel_history_is_converted_to_pairs(self):
        messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'a', 'function': {'name': 'first', 'arguments': '{}'}},
            {'id': 'b', 'function': {'name': 'second', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': '{"a":1}'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': '{"b":2}'}]
        result = to_gigachat_messages(messages, False)
        self.assertEqual([m['role'] for m in result], ['assistant', 'function', 'assistant', 'function'])
        self.assertEqual([m['name'] for m in result if m['role'] == 'function'], ['first', 'second'])

    def test_search_is_not_offered_before_confirmation(self):
        s = session.create_session('Квартира', ScriptedLLM())
        s.profile = {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000, 'payment': 'cash'}
        self.assertNotIn('search_listings', [t.name for t in actions.tools(s)])
        self.assertIn('confirm_red_lines', [t.name for t in actions.tools(s)])
        s.red_lines_ok = True
        self.assertIn('search_listings', [t.name for t in actions.tools(s)])
        self.assertNotIn('confirm_red_lines', [t.name for t in actions.tools(s)])

    def test_repeated_housing_call_does_not_open_card_again(self):
        async def go():
            s = session.create_session('Квартира', ScriptedLLM(call('ask_housing'), call('ask_housing'), say('Ищу')))
            s.profile = {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000, 'payment': 'cash'}
            s.red_lines_ok = True
            await drive(s, {'housing': (True, {'housing': 'both'})})
            return s
        s = asyncio.run(go())
        self.assertEqual(s.profile['housing'], 'both')
        self.assertEqual(len([e for e in s.history if e['type'] == 'confirm' and e['kind'] == 'housing']), 1)
        self.assertNotIn('ask_housing', [t.name for t in actions.tools(s)])
