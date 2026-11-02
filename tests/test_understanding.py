"""Рецензент: исправление смысла, цитаты, неоднозначность, вопросы, кэш и запрет устаревших записей."""
import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import actions, qa, session, understanding as U
from backend.agent.actions import request as R
from backend.agent.profile import merge_profile
from backend.llm import LLMClient, LLMError, LLMResponse, ToolCall


class ReviewModel(LLMClient):
    name, model = 'test', 'review'
    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []
    async def chat(self, messages, **kwargs):
        assert kwargs['tag'] == 'request_review'
        self.seen.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = await answer()
        return LLMResponse(json.dumps(answer, ensure_ascii=False))


def verdict(text, changes=None, intent='refine', decision=None):
    changes = changes or {}
    return {'intent': intent, 'decision': decision or ('apply' if changes else 'unchanged'), 'changes': changes,
            'evidence': [{'field': k, 'quote': text} for k in changes], 'question': '', 'options': []}


class UnderstandingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')

    def agent(self, text, *reviews, profile=None):
        s = session.create_session('', ReviewModel(*reviews))
        s.profile = merge_profile(profile or {})
        s.prefs.sync(s.profile)
        event = s.push('message', role='user', text=text)
        s.accept_request(text, event['seq'])
        return s

    async def test_critic_repairs_dropped_alternatives_and_market_using_the_whole_message(self):
        text = 'Однушка или трёшка, до пяти с половиной лямов, вторичка, наличка, Казань.'
        corrected = {'rooms_options':[1,3], 'budget_max':5_500_000, 'housing':'secondary', 'payment':'cash', 'city':'Казань'}
        s = self.agent(text, verdict(text, corrected, intent='search'))
        await R.update_request(s, rooms=3, budget_max=5_000_000, city='Казань')
        self.assertEqual(s.profile['rooms_options'], [1,3])
        self.assertEqual(s.profile['budget_max'], 5_500_000)
        self.assertEqual(s.profile['housing'], 'secondary')
        self.assertEqual(s.profile['payment'], 'cash')
        payload = json.loads(s.llm.seen[0][1]['content'])['data']
        self.assertEqual(payload['proposed_changes']['rooms'], 3)

    async def test_targeted_cancellation_and_unmentioned_fields_survive(self):
        text = 'Зелень больше не важна, и снизу жить уже можно. Верхний этаж всё ещё исключаю.'
        s = self.agent(text, verdict(text, {'clear_criteria':['poi:park','not_first_floor']}), profile={
            'city':'Самара','rooms_options':[1,2],'budget_max':10_000_000,'housing':'secondary',
            'poi':['park','school'],'soft_criteria':['poi:park'],'not_first_floor':True,'not_last_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'], ['school'])
        self.assertFalse(s.profile['not_first_floor'])
        self.assertTrue(s.profile['not_last_floor'])
        self.assertEqual(s.profile['rooms_options'], [1,2])
        self.assertEqual(s.profile['budget_max'], 10_000_000)

    async def test_ambiguity_does_not_mutate_until_actual_clarification(self):
        text = 'Двушка в Новгороде до 9 миллионов.'
        ambiguous = verdict(text, decision='clarify', intent='search')
        ambiguous.update(question='Великий или Нижний Новгород?', options=['Великий Новгород','Нижний Новгород'])
        answer = 'Великий Новгород'
        final = verdict(answer, {'city':'Великий Новгород','rooms':2,'budget_max':9_000_000}, intent='search')
        final['evidence'] = [{'field':'city','quote':answer},{'field':'rooms','quote':'Двушка'}, {'field':'budget_max','quote':'9 миллионов'}]
        s = self.agent(text, ambiguous, final)
        before = deepcopy(s.profile)
        s.ask = AsyncMock(return_value={'approved':None,'data':{'text':answer}})
        result = await U.ensure(s, {'city':'Нижний Новгород'})
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(s.profile, before)
        event = s.push('message',role='user',text=answer)
        s.accept_request(answer,event['seq'])
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['budget_max']), ('Великий Новгород',9_000_000))

    async def test_missing_evidence_gets_one_repair_and_no_partial_application(self):
        text = 'Квартира в Казани.'
        wrong = verdict('выдуманная цитата', {'city':'Москва'})
        s = self.agent(text, wrong, verdict(text, {'city':'Казань'}))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Казань')
        self.assertEqual(len(s.llm.seen),2)
        # GigaChat допускает system только первым сообщением, включая исправление формата.
        self.assertEqual([m['role'] for m in s.llm.seen[1]], ['system', 'user'])

    async def test_two_invalid_reviews_leave_profile_unchanged(self):
        text = 'Условия прежние.'
        wrong = verdict('нет в исходном сообщении', {'budget_max':999_000_000})
        s = self.agent(text, wrong, wrong, profile={'budget_max':5_000_000})
        before = deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile,before)
        self.assertEqual(len(s.llm.seen),2)

    async def test_non_object_json_is_repaired_once(self):
        text = 'Самара'
        s = self.agent(text, 'not an object', verdict(text, {'city':'Самара'}))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Самара')
        self.assertEqual(len(s.llm.seen),2)

    async def test_confirmed_state_change_during_review_invalidates_the_pending_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        text = 'Казань'
        async def delayed():
            entered.set()
            await release.wait()
            return verdict(text, {'city':'Казань'})
        s = self.agent(text, delayed, profile={'city':'Москва'})
        task = asyncio.create_task(U.ensure(s))
        await entered.wait()
        s.profile['payment'] = 'cash'
        s.client_revision += 1
        release.set()
        with self.assertRaises(U.UnderstandingError):
            await task
        self.assertEqual(s.profile['city'],'Москва')
        self.assertEqual(s.profile['payment'],'cash')

    async def test_cached_review_is_refreshed_after_a_structured_confirmation(self):
        text = 'Казань'
        s = self.agent(text, verdict(text, {'city':'Казань'}), verdict(text, {'city':'Казань'}), profile={'city':'Москва'})
        await U.prepare(s)
        s.profile['payment'] = 'cash'
        await U.ensure(s)
        self.assertEqual(len(s.llm.seen),2)
        self.assertEqual((s.profile['city'],s.profile['payment']),('Казань','cash'))

    async def test_review_cache_cannot_be_overwritten_by_a_later_planner_proposal(self):
        text = 'Потолок девять миллионов.'
        s = self.agent(text, verdict(text, {'budget_max':9_000_000}))
        await R.update_request(s,budget_max=10_000_000)
        await R.update_request(s,budget_max=100_000_000)
        self.assertEqual(s.profile['budget_max'],9_000_000)
        self.assertEqual(len(s.llm.seen),1)

    async def test_question_shaped_refinement_changes_request_instead_of_going_to_faq(self):
        text = 'Можно ли вместо Москвы смотреть Казань?'
        s = self.agent('старый ввод', verdict(text, {'city':'Казань'}), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        s.request_review_seq = s.request_inputs[-1]['seq']
        future = asyncio.get_running_loop().create_future()
        s.pending['selection'] = future
        self.assertEqual(await s.receive_text(text),'answer')
        self.assertTrue(future.done())
        self.assertEqual(s.profile['city'],'Москва')  # HTTP-классификация не пишет состояние посреди инструмента
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Казань')

    async def test_faq_keeps_confirmation_open_and_does_not_change_profile(self):
        text = 'Что такое эскроу?'
        s = self.agent('старый ввод', verdict(text,intent='question'), profile={'city':'Москва'})
        s.request_review_seq = s.request_inputs[-1]['seq']
        future = asyncio.get_running_loop().create_future()
        s.pending['price'] = future
        before = deepcopy(s.profile)
        with patch.object(qa,'answer',AsyncMock()) as answer:
            self.assertEqual(await s.receive_text(text),'question')
            await asyncio.sleep(0)
            answer.assert_awaited_once()
        self.assertFalse(future.done())
        self.assertEqual(s.profile,before)

    async def test_unavailable_reviewer_keeps_open_confirmation_and_existing_profile(self):
        text = 'Поменяй город.'
        s = self.agent('старый ввод', LLMError('offline'), profile={'city':'Москва'})
        s.request_review_seq = s.request_inputs[-1]['seq']
        future = asyncio.get_running_loop().create_future()
        s.pending['price'] = future
        self.assertEqual(await s.receive_text(text),'unresolved')
        self.assertFalse(future.done())
        self.assertEqual(s.profile['city'],'Москва')

    async def test_new_input_during_review_cannot_apply_the_stale_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        text = 'Казань'
        async def delayed():
            entered.set()
            await release.wait()
            return verdict(text, {'city':'Казань'})
        s = self.agent(text, delayed, profile={'city':'Москва'})
        task = asyncio.create_task(U.ensure(s))
        await entered.wait()
        event = s.push('message',role='user',text='Нет, Самара')
        s.accept_request(event['text'],event['seq'])
        release.set()
        with self.assertRaises(U.UnderstandingError):
            await task
        self.assertEqual(s.profile['city'],'Москва')

    async def test_new_requirements_revoke_old_call_authority_before_tool_permission_check(self):
        text = 'Теперь ищем в Казани.'
        s = self.agent(text, verdict(text, {'city':'Казань'}), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        s.red_lines_ok = True
        with patch.object(sources,'verify',AsyncMock()) as verify:
            result = await actions.execute(s,ToolCall('call_seller',{'listing_id':'old'}))
            self.assertIn('error',result)
            verify.assert_not_awaited()
        self.assertFalse(s.red_lines_ok)

    async def test_explicit_hold_prevents_work_even_with_a_complete_profile(self):
        text = 'Пока не ищи, мне надо подумать.'
        s = self.agent(text, verdict(text,intent='hold'), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        await U.ensure(s)
        self.assertFalse(actions.available(s,'search_listings'))
        self.assertFalse(actions.available(s,'call_seller'))

    def test_machine_budget_is_already_in_rubles_and_is_not_guessed_from_its_magnitude(self):
        self.assertEqual(merge_profile({'budget_max':50_000})['budget_max'],50_000)
