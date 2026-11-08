"""Структурированный ввод: неоднозначность, вопросы, кэш и запрет устаревших записей."""
import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import actions, qa, session, understanding as U
from backend.agent.profile import merge_profile
from backend.agent import input_frame
from backend.agent.request_schema import CHANGE_SCHEMA
from backend.llm import LLMClient, LLMError, LLMResponse, ToolCall
from tests.input_contract import input_reply as reply, slots


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
        # Краткие unit-фикстуры slots не знают текст: снабжаем их источником.
        if isinstance(answer,dict) and isinstance(answer.get('fields'),dict):
            text=json.loads(messages[-1]['content'])['data']['messages'][-1]['text']
            answer=deepcopy(answer)
            for slot in answer['fields'].values():
                if isinstance(slot,dict) and slot.get('source')=='':slot['source']=text
        return LLMResponse(json.dumps(answer, ensure_ascii=False))




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

    async def test_short_correction_gets_previous_query_and_dialogue(self):
        from backend.agent.listing_query import default_query
        query={**default_query(),'limit':4,'sort':[{'field':'area','direction':'desc'}]}
        s=self.agent('4 а не 3',slots(intent='selection',selection=query),profile={'city':'Москва','rooms':2})
        s.selection_query={**query,'limit':3}
        s.history.insert(0,{'type':'message','role':'user','text':'Дай три самых больших'})
        await U.ensure(s)
        payload=json.loads(s.llm.seen[0][-1]['content'])['data']
        self.assertEqual(payload['workflow']['selection_query']['limit'],3)
        self.assertEqual(payload['recent_dialogue'][0]['text'],'Дай три самых больших')
        self.assertEqual(s.selection_query['limit'],4)
        self.assertEqual(s.profile['rooms'],2)

    async def test_unsupported_condition_pauses_and_explains_without_claiming_absence(self):
        text='Хочу квартиру где внутри бассейн'
        frame=slots(intent='refine',criteria={'poi:fitness':'required'},fields={'children':0},
                    unsupported=[{'text':'внутри бассейн','source':text,'action':'add'}])
        s=self.agent(text,frame,profile={'city':'Москва','rooms':2,'budget_max':20_000_000})
        s.call_targets=['old']
        s.ask=AsyncMock(return_value={'approved':None,'data':{'text':'Самые просторные'}})
        result=await U.ensure(s)
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(s.profile['rooms'],2)
        self.assertEqual(s.request_intent,'hold')
        self.assertFalse(s.call_targets)
        self.assertNotIn('fitness',s.profile['poi'])
        self.assertIsNone(s.profile['children'])
        self.assertIn('внутри бассейн',s.profile['_unsupported_conditions'])
        message=next(e['text'] for e in s.history if e.get('role')=='agent')
        self.assertIn('Пока не умею',message)
        self.assertIn('не означает, что таких квартир нет',message)
        self.assertIn('самые просторные',message)
        self.assertIn('бассейн',s.ask.call_args.args[2])

    async def test_unsupported_initial_request_saves_known_facts_without_starting_search(self):
        from backend.agent.workflow import next_search_action
        text='Москва, двушка до 20 млн за свои, внутри бассейн'
        frame=slots(places=['Москва'],fields={'rooms':2,'budget_max':20_000_000,'payment':'cash'},intent='search',
                    unsupported=[{'text':'внутри бассейн','source':text,'action':'add'}])
        s=self.agent(text,frame)
        s.ask=AsyncMock(return_value={'approved':None,'data':{'text':'Самые выгодные'}})
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['rooms'],s.profile['budget_max']),('Москва',2,20_000_000))
        self.assertIsNone(next_search_action(s))
        self.assertIsNone(s.search)

    async def test_unsupported_condition_can_be_removed_during_a_selection_command(self):
        from backend.agent.listing_query import default_query
        text='Бассейн больше не нужен, покажи три лучших'
        frame=slots(intent='selection',selection={**default_query(),'limit':3},
                    unsupported=[{'text':'внутри бассейн','source':text,'action':'remove'}])
        s=self.agent(text,frame,profile={'city':'Москва','rooms':2})
        s.profile['_unsupported_conditions']=['внутри бассейн']
        await U.ensure(s)
        self.assertFalse(s.profile['_unsupported_conditions'])
        self.assertEqual(s.selection_query['limit'],3)

    async def test_criterion_and_priority_are_applied_together_in_one_call(self):
        s=self.agent('Парк желателен, первый и последний этаж запрещены.', slots(criteria={'poi:park':'preferred','not_first_floor':'required','not_last_floor':'required'}))
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

    async def test_declined_city_and_single_room_count_use_the_native_contract(self):
        value=slots(fields={'rooms':2,'budget_max':15_000_000},places=['Москва'],intent='search')
        value['places'][0]['words'][0]['source']='Москве'
        s=self.agent('Ищу двухкомнатную квартиру в Москве до 15 млн ₽',value)
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['rooms'],s.profile['budget_max']),('Москва',2,15_000_000))
        sent=json.loads(s.llm.seen[0][-1]['content'])
        # Источник выбирается из буквальных слов сообщения в нативной схеме.
        self.assertIn('Москве',str(sent))
        contract=input_frame.schema(CHANGE_SCHEMA,s.request_inputs)
        words=contract['properties']['places']['items']['properties']['words']['items']['properties']['source']['enum']
        self.assertIn('Москве',words)
        self.assertNotIn('Москва',words)
        validation_schema=contract['properties']['fields']['properties']['rooms_options']['properties']['value']
        self.assertEqual(validation_schema['items']['minimum'],1)
        self.assertNotIn('rooms',contract['properties']['fields']['properties'])

    async def test_qualitative_wish_without_a_numeric_limit_does_not_reject_other_fields(self):
        value=slots(fields={'rooms':2,'wishes':['рядом с метро']},places=['Москва'],
                    criteria={'metro_max_minutes':{'value':None,'priority':'required'}},intent='search')
        value['places'][0]['words'][0]['source']='Москве'
        s=self.agent('Ищу двухкомнатную квартиру в Москве рядом с метро',value)
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['rooms']),('Москва',2))
        self.assertIsNone(s.profile['metro_max_minutes'])
        self.assertIn('рядом с метро',s.profile['wishes'])
        self.assertIsNone(s.profile['budget_max'])

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
        s=self.agent('Парк не нужен, первый можно, последний нельзя.',slots(criteria={'poi:park':'remove','not_first_floor':'remove','not_last_floor':'required'}),profile={
                'poi':['park','school'],'soft_criteria':['poi:park','poi:school'],'not_first_floor':True,'not_last_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['school'])
        self.assertEqual(s.profile['soft_criteria'],['poi:school'])
        self.assertFalse(s.profile['not_first_floor'])
        self.assertTrue(s.profile['not_last_floor'])

    async def test_required_priority_removes_previous_soft_flag_without_removing_criterion(self):
        s=self.agent('Теперь парк обязателен.',slots(criteria={'poi:park':'required'}),profile={
            'poi':['park'],'soft_criteria':['poi:park']})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'],['park'])
        self.assertEqual(s.profile['soft_criteria'],[])

    async def test_catalogue_reference_produces_clarification_without_partial_writes(self):
        value=slots(places=['Новгород'],fields={'rooms_options':[2],'budget_max':9_000_000},intent='search')
        value['places'][0]['words'][0]['source']='Новгороде'
        s=self.agent('Двушка в Новгороде до девяти миллионов.',value)
        before=deepcopy(s.profile)
        review=await U.prepare(s)
        self.assertEqual(review['decision'],'clarify')
        self.assertEqual(set(review['options']),{'Великий Новгород','Нижний Новгород'})
        self.assertEqual(s.profile,before)
        self.assertEqual(review['changes'],{})

    async def test_frame_fields_keep_room_alternatives_and_ruble_values(self):
        s=self.agent('Казань, однушка или трёшка, пять с половиной лямов.',slots(places=['Казань'],fields={
            'rooms_options':[1,3],'budget_max':5_500_000,'housing':'secondary','payment':'cash'},intent='search'))
        await U.ensure(s)
        self.assertEqual(s.profile['rooms_options'],[1,3])
        self.assertEqual(s.profile['budget_max'],5_500_000)
        self.assertEqual(s.profile['city'],'Казань')

    async def test_information_frame_keeps_current_confirmation_open(self):
        s=self.agent('старый ввод',slots(intent='question'),profile={'city':'Казань'})
        s.request_review_seq=s.request_inputs[-1]['seq']
        future=asyncio.get_running_loop().create_future()
        s.pending['red_lines']=future
        with patch.object(qa,'answer',AsyncMock()):
            self.assertEqual(await s.receive_text('Что такое эскроу?'),'question')
            await asyncio.sleep(0)
        self.assertFalse(future.done())
        self.assertEqual(s.profile['city'],'Казань')

    async def test_information_intent_has_no_authority_to_apply_repeated_or_invented_fields(self):
        value=slots(places=['Москва'],fields={'budget_max':100_000_000},intent='question')
        s=self.agent('Что такое эскроу?',value,profile={'city':'Казань','budget_max':9_000_000})
        before=deepcopy(s.profile)
        await U.ensure(s)
        self.assertEqual(s.profile,before)
        self.assertEqual(s.request_intent,'question')

    async def test_continuation_and_other_cannot_broaden_existing_profile(self):
        for intent in ('continue', 'other'):
            with self.subTest(intent=intent):
                value=slots(fields={'rooms_options':[0,1,2,3,4,5], 'budget_max':100_000_000},intent=intent)
                s=self.agent('Продолжайте прежнее.',value,profile={'city':'Казань','rooms':2,'budget_max':9_000_000})
                before=deepcopy(s.profile)
                await U.ensure(s)
                self.assertEqual(s.profile,before)
                self.assertEqual(s.request_intent,intent)

    async def test_a_single_source_name_cannot_grow_an_unspoken_city_modifier(self):
        value=slots(places=['Новгород'])
        value['places'][0]['words'][0]={'source':'Новгороде','lemma':'Великий Новгород'}
        s=self.agent('Квартира в Новгороде',value,value)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_catalogue_word_order_does_not_change_the_resolved_city(self):
        s=self.agent('Великий Новгород',slots(places=['Новгород Великий']))
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

    async def test_only_the_current_object_contract_is_accepted(self):
        legacy = [
            {'fields': [{'field':'budget_max','value':5_000_000,'source_seq':1}], 'criteria':[], 'places':[], 'intent':'refine','question':'','options':[]},
            {'intent':'refine','decision':'apply','changes':{'budget_max':5_000_000},'evidence':[]},
        ]
        for value in legacy:
            with self.subTest(value=value):
                s=self.agent('Пять миллионов.',value,value)
                before=deepcopy(s.profile)
                with self.assertRaises(U.UnderstandingError): await U.ensure(s)
                self.assertEqual(s.profile,before)

    async def test_later_correction_is_extracted_from_all_pending_messages(self):
        s=self.agent('До пяти миллионов.',slots(fields={'budget_max':4_000_000}))
        event=s.push('message',role='user',text='Передумал, до четырёх.')
        s.accept_request(event['text'],event['seq'])
        await U.ensure(s)
        payload=json.loads(s.llm.seen[0][1]['content'])['data']
        self.assertEqual([m['text'] for m in payload['messages']],['До пяти миллионов.','Передумал, до четырёх.'])
        self.assertEqual(s.profile['budget_max'],4_000_000)

    async def test_conflicting_values_require_clarification_without_partial_writes(self):
        value=slots(fields={'budget_max':4_000_000})
        value.update(question='Какой потолок: четыре или пять миллионов?',options=['4 млн','5 млн'])
        s=self.agent('Четыре или пять, ещё не решил.',value)
        before=deepcopy(s.profile)
        review=await U.prepare(s)
        self.assertEqual(review['decision'],'clarify')
        self.assertEqual(review['changes'],{})
        self.assertEqual(s.profile,before)

    async def test_understanding_uses_the_whole_message_without_trusting_planner_proposals(self):
        text = 'Однушка или трёшка, до пяти с половиной лямов, вторичка, наличка, Казань.'
        corrected = {'rooms_options':[1,3], 'budget_max':5_500_000, 'housing':'secondary', 'payment':'cash', 'city':'Казань'}
        s = self.agent(text, reply(text, corrected, intent='search'))
        await actions.execute(s,ToolCall('update_request',{'rooms':3,'budget_max':5_000_000,'city':'Казань'}))
        self.assertEqual(s.profile['rooms_options'], [1,3])
        self.assertEqual(s.profile['budget_max'], 5_500_000)
        self.assertEqual(s.profile['housing'], 'secondary')
        self.assertEqual(s.profile['payment'], 'cash')
        payload = json.loads(s.llm.seen[0][1]['content'])['data']
        self.assertNotIn('proposed_changes', payload)

    async def test_targeted_cancellation_and_unmentioned_fields_survive(self):
        text = 'Зелень больше не важна, и снизу жить уже можно. Верхний этаж всё ещё исключаю.'
        s = self.agent(text, reply(text, {'clear_criteria':['poi:park','not_first_floor']}), profile={
            'city':'Самара','rooms_options':[1,2],'budget_max':10_000_000,'housing':'secondary',
            'poi':['park','school'],'soft_criteria':['poi:park'],'not_first_floor':True,'not_last_floor':True})
        await U.ensure(s)
        self.assertEqual(s.profile['poi'], ['school'])
        self.assertFalse(s.profile['not_first_floor'])
        self.assertTrue(s.profile['not_last_floor'])
        self.assertEqual(s.profile['rooms_options'], [1,2])
        self.assertEqual(s.profile['budget_max'], 10_000_000)




    async def test_unknown_machine_fields_are_rejected_without_partial_writes(self):
        value=slots(fields={'budget_max':5_000_000,'invented':True})
        s=self.agent('Пять миллионов.',value,value)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_invalid_criterion_priority_is_rejected_atomically(self):
        value=slots(fields={'budget_max':5_000_000},criteria={'poi:park':'maybe'})
        s=self.agent('Пять миллионов, парк желателен.',value,value)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_ambiguity_does_not_mutate_until_actual_clarification(self):
        text = 'Двушка в Новгороде до 9 миллионов.'
        ambiguous = slots(intent='search')
        ambiguous.update(question='Великий или Нижний Новгород?', options=['Великий Новгород','Нижний Новгород'])
        answer = 'Великий Новгород'
        final = reply(answer, {'city':'Великий Новгород','rooms':2,'budget_max':9_000_000}, intent='search')
        s = self.agent(text, ambiguous, final)
        before = deepcopy(s.profile)
        s.ask = AsyncMock(return_value={'approved':None,'data':{'text':answer}})
        result = await actions.execute(s,ToolCall('update_request',{'city':'Нижний Новгород'}))
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(s.profile, before)
        event = s.push('message',role='user',text=answer)
        s.accept_request(answer,event['seq'])
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['budget_max']), ('Великий Новгород',9_000_000))

    async def test_unspoken_city_gets_one_repair_and_no_partial_application(self):
        text='Квартира в Казани.'
        s=self.agent(text,slots(places=['Москва']),reply(text,{'city':'Казань'}))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Казань')
        self.assertEqual(len(s.llm.seen),2)
        self.assertEqual([m['role'] for m in s.llm.seen[1]],['system','user'])

    async def test_unmentioned_fields_are_preserved(self):
        text='Теперь Казань, бюджет прежний.'
        value=reply(text,{'city':'Казань'})
        s=self.agent(text,value,profile={'city':'Москва','budget_max':9_000_000})
        await U.ensure(s)
        self.assertEqual((s.profile['city'],s.profile['budget_max']),('Казань',9_000_000))
        self.assertEqual(len(s.llm.seen),1)

    async def test_machine_criteria_do_not_need_literal_quotes(self):
        s=self.agent('Первый и последний этаж не подходят.',slots(criteria={
            'not_first_floor':'required','not_last_floor':'required'}))
        await U.ensure(s)
        self.assertTrue(s.profile['not_first_floor'] and s.profile['not_last_floor'])

    async def test_source_must_be_a_whole_word_not_a_substring(self):
        value=slots(places=['Казань'])
        value['places'][0]['words'][0]['source']='Каз'
        s=self.agent('Казань',value,value)
        before=deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):await U.ensure(s)
        self.assertEqual(s.profile,before)

    async def test_two_invalid_reviews_leave_profile_unchanged(self):
        text = 'Условия прежние.'
        wrong = slots(fields={'budget_max':'не число'})
        s = self.agent(text, wrong, wrong, profile={'budget_max':5_000_000})
        before = deepcopy(s.profile)
        with self.assertRaises(U.UnderstandingError):
            await U.ensure(s)
        self.assertEqual(s.profile,before)
        self.assertEqual(len(s.llm.seen),2)

    async def test_non_object_json_is_repaired_once(self):
        text = 'Самара'
        s = self.agent(text, 'not an object', reply(text, {'city':'Самара'}))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Самара')
        self.assertEqual(len(s.llm.seen),2)

    async def test_sparse_response_does_not_need_unused_question_metadata(self):
        value=slots(places=['Казань'])
        del value['question'],value['options']
        s=self.agent('Казань',value,slots(places=['Казань']))
        await U.ensure(s)
        self.assertEqual(s.profile['city'],'Казань')
        self.assertEqual(len(s.llm.seen),1)

    async def test_missing_actual_clarification_question_still_fails_closed(self):
        text = 'Новгород'
        value = slots()
        del value['question'], value['options']
        s = self.agent(text, value, value)
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
            return reply(text, {'city':'Казань'})
        s = self.agent(text, delayed, profile={'city':'Москва'})
        task = asyncio.create_task(U.ensure(s))
        await entered.wait()
        s.profile['payment'] = 'cash'
        s.client_revision += 1
        release.set()
        with self.assertRaises(session.Superseded):
            await task
        self.assertEqual(s.profile['city'],'Москва')
        self.assertEqual(s.profile['payment'],'cash')

    async def test_cached_review_is_refreshed_after_a_structured_confirmation(self):
        text = 'Казань'
        s = self.agent(text, reply(text, {'city':'Казань'}), reply(text, {'city':'Казань'}), profile={'city':'Москва'})
        await U.prepare(s)
        s.profile['payment'] = 'cash'
        await U.ensure(s)
        self.assertEqual(len(s.llm.seen),2)
        self.assertEqual((s.profile['city'],s.profile['payment']),('Казань','cash'))

    async def test_review_cache_cannot_be_overwritten_by_a_later_planner_proposal(self):
        text = 'Потолок девять миллионов.'
        s = self.agent(text, reply(text, {'budget_max':9_000_000}))
        await actions.execute(s,ToolCall('update_request',{'budget_max':10_000_000}))
        await actions.execute(s,ToolCall('update_request',{'budget_max':100_000_000}))
        self.assertEqual(s.profile['budget_max'],9_000_000)
        self.assertEqual(len(s.llm.seen),1)

    async def test_question_shaped_refinement_changes_request_instead_of_going_to_faq(self):
        text = 'Можно ли вместо Москвы смотреть Казань?'
        s = self.agent('старый ввод', reply(text, {'city':'Казань'}), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
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
        s = self.agent('старый ввод', reply(text,intent='question'), profile={'city':'Москва'})
        s.request_review_seq = s.request_inputs[-1]['seq']
        future = asyncio.get_running_loop().create_future()
        s.pending['price'] = future
        before = deepcopy(s.profile)
        with patch.object(qa,'reply',AsyncMock(return_value='Ответ на вопрос')) as answer:
            self.assertEqual(await s.receive_text(text),'question')
            await s.qa_task
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
        messages=[e for e in s.history if e['type']=='message' and e.get('role')=='agent']
        self.assertTrue(messages[-1]['await_reply'])

    async def test_new_input_during_review_cannot_apply_the_stale_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        text = 'Казань'
        async def delayed():
            entered.set()
            await release.wait()
            return reply(text, {'city':'Казань'})
        s = self.agent(text, delayed, profile={'city':'Москва'})
        task = asyncio.create_task(U.ensure(s))
        await entered.wait()
        event = s.push('message',role='user',text='Нет, Самара')
        s.accept_request(event['text'],event['seq'])
        release.set()
        with self.assertRaises(session.Superseded):
            await task
        self.assertEqual(s.profile['city'],'Москва')

    async def test_new_requirements_revoke_old_call_authority_before_tool_permission_check(self):
        text = 'Теперь ищем в Казани.'
        s = self.agent(text, reply(text, {'city':'Казань'}), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        s.red_lines_ok = True
        with patch.object(sources,'verify',AsyncMock()) as verify:
            result = await actions.execute(s,ToolCall('call_seller',{'listing_id':'old'}))
            self.assertIn('error',result)
            verify.assert_not_awaited()
        self.assertTrue(s.red_lines_ok)  # бюджет не изменился, но прежняя подборка отозвана
        self.assertIsNone(s.search)

    async def test_invalid_planner_argument_types_do_not_block_a_valid_semantic_request(self):
        text='Казань, однушка до пяти миллионов.'
        s=self.agent(text,reply(text,{'city':'Казань','rooms':1,'budget_max':5_000_000},intent='search'))
        result=await actions.execute(s,ToolCall('update_request',{'city':123,'budget_max':'пять миллионов'}))
        self.assertNotIn('error',result)
        self.assertEqual((s.profile['city'],s.profile['rooms'],s.profile['budget_max']),('Казань',1,5_000_000))

    async def test_explicit_hold_prevents_work_even_with_a_complete_profile(self):
        text = 'Пока не ищи, мне надо подумать.'
        s = self.agent(text, reply(text,intent='hold'), profile={'city':'Москва','rooms':1,'budget_max':8_000_000})
        await U.ensure(s)
        self.assertFalse(actions.available(s,'search_listings'))
        self.assertFalse(actions.available(s,'call_seller'))

    def test_machine_budget_is_already_in_rubles_and_is_not_guessed_from_its_magnitude(self):
        self.assertEqual(merge_profile({'budget_max':50_000})['budget_max'],50_000)


class MillionCitiesInputTest(unittest.IsolatedAsyncioTestCase):
    """Тест 08.11: «двушка в Уфе…» падала с «places: Слишком много элементов» — «Уфе» не узнавалась как город,
    и схема запрещала модели называть место. Проверяем каждый город-миллионник в предложном падеже."""
    def setUp(self):
        db.configure('sqlite://')

    async def review(self, text, changes):
        """Как DeepSeek/GigaChat: маршрут profile_change, затем извлечение условий отдельным запросом (profile_extract)."""
        class Model(LLMClient):
            name, model = 'test', 'tools'
            def __init__(m):m.seen = []
            async def chat(m, messages, **kw):
                m.seen.append(kw['tag'])
                if kw['tag'] == 'request_review':
                    return LLMResponse('', [ToolCall('profile_change', {})])
                fields = [{'key': 'city', 'value': changes['city'], 'source': text}]
                for key in ('rooms_options', 'budget_max', 'district_type'):
                    if key in changes:
                        fields.append({'key': key, 'value': json.dumps(changes[key]) if key == 'rooms_options' else str(changes[key]), 'source': text})
                return LLMResponse(json.dumps({'fields': fields, 'criteria': [], 'states': [], 'filters': [], 'places': [],
                                               'unsupported': [], 'selection': [], 'requested_card': []}, ensure_ascii=False))
        s = session.create_session('', Model())
        event = s.push('message', role='user', text=text)
        s.accept_request(text, event['seq'])
        await U.ensure(s)
        return s

    async def test_user_message_from_test_drive(self):
        text = 'Ищу двухкомнатную квартиру в Уфе за 12 миллионов рублей. Для меня важен спальный район и близость к поликлинике.'
        s = await self.review(text, {'city': 'Уфа', 'rooms_options': [2], 'budget_max': 12_000_000, 'district_type': 'sleeping'})
        self.assertEqual((s.profile['city'], s.profile['rooms'], s.profile['budget_max'], s.profile['district_type']),
                         ('Уфа', 2, 12_000_000, 'sleeping'))
        self.assertEqual(s.llm.seen, ['request_review', 'profile_extract'])  # с первой попытки, без повтора

    async def test_every_million_city_in_locative(self):
        import pymorphy3
        from backend.geodata import CITIES
        morph = pymorphy3.MorphAnalyzer()

        def loct(name):
            if name == 'Ростов-на-Дону':
                return 'Ростове-на-Дону'
            if name == 'Санкт-Петербург':
                return 'Санкт-Петербурге'
            return ' '.join(morph.parse(w)[0].inflect({'loct'}).word.capitalize() for w in name.split())
        for c in CITIES:
            with self.subTest(city=c['name']):
                text = f'Двушка в {loct(c["name"])} до 10 млн'
                self.assertEqual(sources.normalize_city(text), c['name'])
                s = await self.review(text, {'city': c['name']})
                self.assertEqual(s.profile['city'], c['name'])
