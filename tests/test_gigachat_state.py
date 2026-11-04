"""Состояние функций GigaChat и доступность действий вместо повторных карточек."""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock

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
            from backend import sources
            from tests.test_llm_and_agent import listing
            with patch.object(sources, 'collect', AsyncMock(return_value=([listing()], []))), patch.object(sources, 'more', AsyncMock(return_value=[])):
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

    def test_free_text_question_marks_waiting_for_user_without_marking_action_narration(self):
        async def go():
            s = session.create_session('Квартира', ScriptedLLM(say('Продолжать поиск в этом бюджете?')))
            await s.say('Ищу объявления')
            await drive(s)
            return s
        s = asyncio.run(go())
        narration, question = [e for e in s.history if e['type'] == 'message' and e['role'] == 'agent']
        self.assertNotIn('await_reply', narration)
        self.assertTrue(question['await_reply'])

    def test_early_server_history_provider_state_is_preserved(self):
        messages = [{'role': 'assistant', 'content': '', 'provider_state': STATE, 'tool_calls': [
            {'id': 'a', 'function': {'name': 'ask_housing', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': '{"housing":"both"}'}]
        result = to_gigachat_messages(messages, False)
        self.assertEqual(result[0]['functions_state_id'], STATE)
        self.assertNotIn('provider_state', result[0])

    def test_old_deepseek_parallel_history_is_converted_to_pairs(self):
        messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'a', 'function': {'name': 'first', 'arguments': '{}'}},
            {'id': 'b', 'function': {'name': 'second', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': '{"a":1}'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': '{"b":2}'}]
        result = to_gigachat_messages(messages, False)
        self.assertEqual([m['role'] for m in result], ['assistant', 'function', 'assistant', 'function'])
        self.assertEqual([m['name'] for m in result if m['role'] == 'function'], ['first', 'second'])

    def test_search_is_offered_after_initial_limit_confirmation(self):
        s = session.create_session('Квартира', ScriptedLLM())
        s.profile = {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000, 'payment': 'cash'}
        self.assertNotIn('search_listings', [t.name for t in actions.tools(s)])
        self.assertNotIn('call_seller', [t.name for t in actions.tools(s)])
        self.assertIn('confirm_red_lines', [t.name for t in actions.tools(s)])
        s.red_lines_ok = True
        self.assertIn('search_listings', [t.name for t in actions.tools(s)])
        self.assertNotIn('confirm_red_lines', [t.name for t in actions.tools(s)])

    def test_confirmed_request_moves_to_search_before_optional_questionnaire(self):
        s = session.create_session('Квартира', ScriptedLLM())
        s.profile = {'city': 'Москва', 'rooms': 2, 'budget_max': 17_500_000, 'payment': 'mortgage_ready'}
        self.assertNotIn('ask_client', [t.name for t in actions.tools(s)])
        s.red_lines_ok = True
        self.assertNotIn('ask_client', [t.name for t in actions.tools(s)])
        self.assertIn('ask_family', [t.name for t in actions.tools(s)])
        s.search = {'pool': []}
        self.assertIn('ask_client', [t.name for t in actions.tools(s)])

    def test_repeated_housing_call_does_not_open_card_again(self):
        async def go():
            s = session.create_session('Квартира', ScriptedLLM(call('ask_housing'), call('ask_housing'), say('Ищу')))
            s.profile = {'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000, 'payment': 'cash'}
            s.red_lines_ok = True
            from tests.test_llm_and_agent import listing
            from backend import sources
            with patch.object(sources, 'collect', AsyncMock(return_value=([listing()], []))), patch.object(sources, 'more', AsyncMock(return_value=[])):
                await drive(s, {'housing': (True, {'housing': 'both'})})
            return s
        s = asyncio.run(go())
        self.assertEqual(s.profile['housing'], 'both')
        self.assertEqual(len([e for e in s.history if e['type'] == 'confirm' and e['kind'] == 'housing']), 1)
        self.assertNotIn('ask_housing', [t.name for t in actions.tools(s)])
