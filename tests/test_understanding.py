"""Структурированный ввод: неоднозначность, вопросы, кэш и запрет устаревших записей."""
import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import actions, qa, session, understanding as U
from backend.agent.actions import request as R
from backend.agent.profile import merge_profile
from backend.agent import input_frame
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


def frame(*, places=None, fields=None, criteria=None, intent='refine'):
    return {'places':[{'words':[{'source':w,'lemma':w} for w in x.split()],'source_seq':1} for x in places or []],
            'fields':[{'field':k,'value':v,'source_seq':1} for k,v in (fields or {}).items()],
            'criteria':[{'key':k,'value':v,'priority':p,'source_seq':1} for k,v,p in criteria or []],
            'question':'','options':[],'intent':intent}


def slots(*, fields=None, criteria=None, places=None, intent='refine'):
    schema=input_frame.schema(U._schema())
    return {'places':[{'words':[{'source':w,'lemma':w} for w in x.split()]} for x in places or []],
            'fields':{**{k:None for k in schema['properties']['fields']['properties']},**(fields or {})},
            'criteria':{**{k:None for k in schema['properties']['criteria']['properties']},**(criteria or {})},
            'question':'','options':[],'intent':intent}




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

    async def test_criterion_and_priority_are_applied_together_in_one_call(self):
        s=self.agent('Парк желателен, первый и последний этаж запрещены.', frame(criteria=[
            ('poi:park',True,'preferred'),('not_first_floor',True,'required'),('not_last_floor',True,'required')]))
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['park'])
        self.assertEqual(s.profile['soft_criteria'],['poi:park'])
        self.assertTrue(s.profile['not_first_floor'] and s.profile['not_last_floor'])
        self.assertEqual(len(s.llm.seen),1)

    async def test_nullable_slots_do_not_invent_constraints_and_preserve_priority(self):
        s=self.agent('Самара, однушка или двушка до 10 млн, парк желателен.',slots(places=['Самара'],fields={
            'rooms_options':[1,2],'budget_max':10_000_000,'housing':'secondary','payment':'cash'},
            criteria={'poi:park':'preferred','not_first_floor':'required','not_last_floor':'required'},intent='search'))
        await U.ensure(s)
        self.assertEqual(s.profile['rooms_options'],[1,2])
        self.assertEqual(s.profile['poi'],['park'])
        self.assertEqual(s.profile['soft_criteria'],['poi:park'])
        self.assertIsNone(s.profile['metro_max_minutes'])
        self.assertEqual(len(s.llm.seen),1)

    async def test_nullable_slots_clear_only_named_conditions(self):
        s=self.agent('Парк уже не нужен, первый этаж можно.',slots(criteria={
            'poi:park':'remove','not_first_floor':'remove'}),profile={
            'poi':['park','school'],'soft_criteria':['poi:park'],'not_first_floor':True,'not_last_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['school'])
        self.assertEqual(s.profile['soft_criteria'],[])
        self.assertFalse(s.profile['not_first_floor'])
        self.assertTrue(s.profile['not_last_floor'])

    async def test_unchanged_geography_repeated_from_profile_does_not_require_another_question(self):
        s=self.agent('Первый этаж теперь можно.',slots(places=['Самара'],criteria={'not_first_floor':'remove'}),profile={
            'city':'Самара','rooms':2,'budget_max':9_000_000,'not_first_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Самара')
        self.assertFalse(s.profile['not_first_floor'])

    async def test_typed_removal_preserves_other_conditions_and_priorities(self):
        s=self.agent('Парк не нужен, первый можно, последний нельзя.',frame(criteria=[
            ('poi:park',None,'remove'),('not_first_floor',False,'required'),('not_last_floor',True,'required')]),profile={
                'poi':['park','school'],'soft_criteria':['poi:park','poi:school'],'not_first_floor':True,'not_last_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['school'])
        self.assertEqual(s.profile['soft_criteria'],['poi:school'])
        self.assertFalse(s.profile['not_first_floor'])
        self.assertTrue(s.profile['not_last_floor'])

    async def test_required_priority_removes_previous_soft_flag_without_removing_criterion(self):
        s=self.agent('Теперь парк обязателен.',frame(criteria=[('poi:park',True,'required')]),profile={
            'poi':['park'],'soft_criteria':['poi:park']})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['park'])
        self.assertEqual(s.profile['soft_criteria'],[])

    async def test_catalogue_reference_produces_clarification_without_partial_writes(self):
        s=self.agent('Двушка в Новгороде до девяти миллионов.',frame(places=['Новгород'],fields={
            'rooms_options':[2],'budget_max':9_000_000},intent='search'))
        before=deepcopy(s.profile)
        review=await U.prepare(s)
        self.assertEqual(review['decision'],'clarify')
        self.assertEqual(set(review['options']),{'Великий Новгород','Нижний Новгород'})
        self.assertEqual(s.profile,before)
        self.assertEqual(review['changes'],{})

    async def test_frame_fields_keep_room_alternatives_and_ruble_values(self):
        s=self.agent('Казань, однушка или трёшка, пять с половиной лямов.',frame(places=['Казань'],fields={
            'rooms_options':[1,3],'budget_max':5_500_000,'housing':'secondary','payment':'cash'},intent='search'))
        await U.ensure(s)
        self.assertEqual(s.profile['rooms_options'],[1,3])
        self.assertEqual(s.profile['budget_max'],5_500_000)
        self.assertEqual(s.profile['city'],'Казань')

    async def test_information_frame_keeps_current_confirmation_open(self):
        s=self.agent('старый ввод',frame(intent='question'),profile={'city':'Казань'})
        s.request_review_seq=s.request_inputs[-1]['seq']
        future=asyncio.get_running_loop().create_future()
        s.pending['red_lines']=future
        with patch.object(qa,'answer',AsyncMock()):
            self.assertEqual(await s.receive_text('Что такое эскроу?'),'question')
            await asyncio.sleep(0)
        self.assertFalse(future.done())
        self.assertEqual(s.profile['city'],'Казань')

    async def test_information_intent_has_no_authority_to_apply_repeated_or_invented_fields(self):
        reply=frame(places=['Москва'],fields={'budget_max':100_000_000},intent='question')
        s=self.agent('Что такое эскроу?',reply,profile={'city':'Казань','budget_max':9_000_000})
        before=deepcopy(s.profile)
        await U.ensure(s)
        self.assertEqual(s.profile,before)
        self.assertEqual(s.request_intent,'question')

    async def test_a_single_source_name_cannot_grow_an_unspoken_city_modifier(self):
        reply=frame(places=['Новгород'])
        reply['places'][0]['words'][0]={'source':'Новгороде','lemma':'Великий Новгород'}
        s=self.agent('Квартира в Новгороде',reply,reply)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_catalogue_word_order_does_not_change_the_resolved_city(self):
        s=self.agent('Великий Новгород',frame(places=['Новгород Великий']))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Великий Новгород')

    async def test_short_answer_resolves_an_existing_catalogue_choice(self):
        s=self.agent('Великий',slots(places=['Великий']))
        s.request_inputs[-1]['options']=['Великий Новгород','Нижний Новгород']
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Великий Новгород')

    async def test_card_options_are_preserved_as_context_for_a_text_reply(self):
        s=session.create_session('',ReviewModel())
        future=asyncio.get_running_loop().create_future()
        s.pending['clarify']=future
        s.push('confirm',kind='clarify',text='Какой город?',chips=['Великий Новгород','Нижний Новгород'])
        event=s.push('message',role='user',text='Великий')
        s.accept_request(event['text'],event['seq'])
        self.assertEqual(s.request_inputs[-1]['options'],['Великий Новгород','Нижний Новгород'])

    async def test_identical_field_repetitions_are_a_single_update(self):
        reply=frame(fields={'budget_max':5_000_000})
        reply['fields'].append(deepcopy(reply['fields'][0]))
        s=self.agent('Пять миллионов, потолок пять.',reply)
        await U.ensure(s)
        self.assertEqual(s.profile['budget_max'],5_000_000)
        self.assertEqual(len(s.llm.seen),1)

    async def test_a_later_message_overrides_an_older_typed_value_even_if_rows_are_reordered(self):
        reply=frame(fields={'budget_max':4_000_000})
        reply['fields'][0]['source_seq']=2
        reply['fields'].append({'field':'budget_max','value':5_000_000,'source_seq':1})
        s=self.agent('До пяти миллионов.',reply)
        event=s.push('message',role='user',text='Передумал, до четырёх.')
        s.accept_request(event['text'],event['seq'])
        await U.ensure(s)
        self.assertEqual(s.profile['budget_max'],4_000_000)

    async def test_conflicting_values_in_one_message_require_clarification(self):
        reply=frame(fields={'budget_max':4_000_000})
        reply['fields'].append({'field':'budget_max','value':5_000_000,'source_seq':1})
        s=self.agent('Четыре или пять, ещё не решил.',reply,reply)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_understanding_uses_the_whole_message_without_trusting_planner_proposals(self):
        text = 'Однушка или трёшка, до пяти с половиной лямов, вторичка, наличка, Казань.'
        corrected = {'rooms_options':[1,3], 'budget_max':5_500_000, 'housing':'secondary', 'payment':'cash', 'city':'Казань'}
        s = self.agent(text, verdict(text, corrected, intent='search'))
        await R.update_request(s, rooms=3, budget_max=5_000_000, city='Казань')
        self.assertEqual(s.profile['rooms_options'], [1,3])
        self.assertEqual(s.profile['budget_max'], 5_500_000)
        self.assertEqual(s.profile['housing'], 'secondary')
        self.assertEqual(s.profile['payment'], 'cash')
        payload = json.loads(s.llm.seen[0][1]['content'])['data']
        self.assertNotIn('proposed_changes', payload)

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




    async def test_sparse_native_fields_are_decoded_and_compared_without_filling_other_fields(self):
        text = 'Казань'
        reply = verdict(text, {'city':'Казань'})
        reply['changes'] = [{'field':'city','value':'Казань'}]
        s=self.agent(text,reply)
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Казань')
        self.assertIsNone(s.profile['budget_max'])
        self.assertIsNone(s.profile['payment'])

    def test_native_sparse_fields_reject_duplicate_updates(self):
        with self.assertRaises(ValueError):
            U._decode_patch([{'field':'city','value':'Москва'},{'field':'city','value':'Казань'}])

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

    async def test_grounded_evidence_for_unchanged_fields_does_not_change_them_or_reject_the_request(self):
        text='Теперь Казань, бюджет прежний.'
        reply=verdict(text,{'city':'Казань'})
        reply['evidence'].append({'field':'budget_max','quote':'бюджет прежний'})
        s=self.agent(text,reply,profile={'city':'Москва','budget_max':9_000_000})
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['budget_max']),('Казань',9_000_000))
        self.assertEqual(len(s.llm.seen),1)

    async def test_source_references_allow_a_paraphrase_without_using_it_as_a_text_parser(self):
        text='Первый и последний этаж не подходят.'
        reply=verdict('Первый этаж запрещён',{'not_first_floor':True,'not_last_floor':True})
        s=self.agent(text,reply)
        for row in reply['evidence']:
            row['source_seq']=s.request_inputs[-1]['seq']
        await U.ensure(s)
        self.assertTrue(s.profile['not_first_floor'] and s.profile['not_last_floor'])

    async def test_a_fabricated_source_reference_is_rejected_even_with_a_valid_quote(self):
        text='Казань'
        reply=verdict(text,{'city':'Казань'})
        reply['evidence'][0]['source_seq']=99999
        s=self.agent(text,reply,reply)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile,before)


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

    async def test_omitted_empty_question_metadata_needs_no_extra_generation(self):
        text = 'Казань'
        reply = verdict(text, {'city':'Казань'})
        del reply['question'], reply['options']
        s = self.agent(text, reply)
        await U.ensure(s)
        self.assertEqual(s.profile['city'], 'Казань')
        self.assertEqual(len(s.llm.seen), 1)

    async def test_missing_actual_clarification_question_still_fails_closed(self):
        text = 'Новгород'
        reply = verdict(text, decision='clarify')
        del reply['question'], reply['options']
        s = self.agent(text, reply, reply)
        before = deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile, before)

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

    async def test_invalid_planner_argument_types_do_not_block_a_valid_semantic_request(self):
        text='Казань, однушка до пяти миллионов.'
        s=self.agent(text,verdict(text,{'city':'Казань','rooms':1,'budget_max':5_000_000},intent='search'))
        result=await actions.execute(s,ToolCall('update_request',{'city':123,'budget_max':'пять миллионов'}))
        self.assertNotIn('error',result)
        self.assertEqual((s.profile['city'],s.profile['rooms'],s.profile['budget_max']),('Казань',1,5_000_000))

    async def test_explicit_hold_prevents_work_even_with_a_complete_profile(self):
        text = 'Пока не ищи, мне надо подумать.'
        s = self.agent(text, verdict(text,intent='hold'), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        await U.ensure(s)
        self.assertFalse(actions.available(s,'search_listings'))
        self.assertFalse(actions.available(s,'call_seller'))

    def test_machine_budget_is_already_in_rubles_and_is_not_guessed_from_its_magnitude(self):
        self.assertEqual(merge_profile({'budget_max':50_000})['budget_max'],50_000)
