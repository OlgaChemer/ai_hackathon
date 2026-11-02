"""Регрессии реального зависания и подбора по альтернативным комнатам/территориям."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend import db, sources
from backend.agent import actions, session
from backend.agent.actions import search as search_actions
from backend.agent.pricing import fair_price, value_rank
from backend.agent.profile import explicit_search_fields, merge_profile, passes_hard_filters
from backend.llm.gigachat import to_gigachat_messages
from backend.sources.base import normalize_city
from tests.fakes import ScriptedLLM, call, drive, say
from tests.test_llm_and_agent import listing


class FlexibleSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')
        self.collect = patch.object(sources, 'collect', AsyncMock(return_value=([listing()], [])))
        self.more = patch.object(sources, 'more', AsyncMock(return_value=[]))
        self.collect_mock = self.collect.start()
        self.more.start()
        self.addCleanup(self.collect.stop)
        self.addCleanup(self.more.stop)

    def agent(self, *steps):
        s = session.create_session('Подберите квартиру', ScriptedLLM(*steps))
        s.profile = merge_profile({'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000})
        s.prefs.sync(s.profile)
        return s

    async def test_narration_without_tool_runs_search_and_selection(self):
        s = self.agent(say('Сейчас начну поиск подходящих квартир.'))
        await drive(s, until=lambda ev: ev['type'] == 'confirm' and ev['kind'] == 'selection')
        self.assertTrue(s.shown)
        self.assertFalse(s.red_lines_ok)  # поиск не даёт разрешение звонить или тратить деньги
        self.assertIsNone(s.profile['payment'])
        self.collect_mock.assert_awaited_once()
        messages = [e.get('text', '') for e in s.history if e['type'] == 'message']
        self.assertNotIn('Сейчас начну поиск подходящих квартир.', messages)
        self.assertTrue(any(e['type'] == 'search_progress' and e['on'] for e in s.history))
        self.assertTrue(any(e['type'] == 'search_progress' and not e['on'] for e in s.history))
        to_gigachat_messages(s.messages, False)  # автоматическое продолжение сохраняет валидные пары вызов/результат
        self.assertNotIn('call_seller', [t.name for t in actions.tools(s)])

    async def test_repeated_known_request_recovers_without_more_from_user(self):
        s = self.agent(*[call('update_request', poi=[]) for _ in range(3)], say('Показываю подборку.'))
        await drive(s, until=lambda ev: ev['type'] == 'confirm' and ev['kind'] == 'selection')
        self.assertTrue(s.shown)
        self.assertFalse(any('остановился' in e.get('text', '') for e in s.history))
        to_gigachat_messages(s.messages, False)

    async def test_empty_secondary_offers_new_buildings_with_buttons_without_switching(self):
        self.collect_mock.return_value = ([], [])
        s = self.agent(call('search_listings'), say('Ничего не нашёл.'))
        s.profile['housing'] = 'secondary'
        events = await drive(s, until=lambda ev: ev['type'] == 'confirm')
        question = next(e for e in events if e['type'] == 'confirm')
        self.assertIn('Посмотреть новостройки', question['chips'])
        self.assertEqual(s.profile['housing'], 'secondary')
        self.assertEqual(s.profile['budget_max'], 9_000_000)

    async def test_finances_are_required_for_deal_preparation_but_not_for_search(self):
        s = self.agent()
        s.chosen, s.red_lines_ok = 'cian-1', True
        self.assertTrue(actions.available(s, 'search_listings'))
        self.assertFalse(actions.available(s, 'prepare_deal'))
        s.profile['payment'] = 'cash'
        self.assertTrue(actions.available(s, 'prepare_deal'))

    async def test_knowledge_answer_does_not_start_a_new_search(self):
        s = self.agent(say('Эскроу — специальный счёт для безопасной сделки.'))
        await drive(s)
        self.collect_mock.assert_not_awaited()


class QueryAndRankingTest(unittest.IsolatedAsyncioTestCase):
    def test_explicit_latest_words_repair_dropped_variants_and_hospital(self):
        changes = explicit_search_fields({'rooms':2, 'poi':['park']}, 'Хочу 1–2 комнаты в Москве или Подмосковье, желательно в центре, важны рядом парк и больница')
        self.assertEqual(changes['rooms_options'], [1,2])
        self.assertEqual(changes['cities'], ['Московская область'])
        self.assertEqual(changes['city'], 'Москва')
        self.assertTrue(changes['prefer_center'])
        self.assertEqual(changes['poi'], ['park','hospital'])
        self.assertEqual(explicit_search_fields({'poi':[]}, 'Больница не нужна')['poi'], [])
        self.assertEqual(explicit_search_fields({'rooms':2}, 'Только две комнаты'), {'rooms':2})
        extras = explicit_search_fields({'metro_max_minutes':15, 'condition':'ready'}, '1–2 комнаты в Самаре, желательно в центре')
        self.assertNotIn('metro_max_minutes', extras)
        self.assertNotIn('condition', extras)
        self.assertEqual(explicit_search_fields({'metro_max_minutes':10}, 'Метро до 10 минут')['metro_max_minutes'], 10)
        self.assertEqual(explicit_search_fields({'metro_max_minutes':10}, '10 минут', criteria_context='Как далеко от метро?')['metro_max_minutes'], 10)

    def test_room_and_city_variants_survive_normalization_and_filters(self):
        p = merge_profile({'city': 'Москва', 'cities': ['Подмосковье'], 'rooms_options': [2, 1, 1], 'budget_max': 10_000_000})
        self.assertEqual(p['rooms_options'], [1, 2])
        self.assertEqual(p['cities'], ['Московская область'])
        self.assertTrue(passes_hard_filters(listing(city='Московская область', rooms=1), p))
        self.assertFalse(passes_hard_filters(listing(city='Москва', rooms=3), p))
        self.assertFalse(passes_hard_filters(listing(city='Казань', rooms=1), p))
        self.assertEqual(merge_profile({'rooms': 3}, p)['rooms_options'], [])
        self.assertEqual(normalize_city('Московская область'), 'Московская область')
        self.assertEqual(normalize_city('Подмосковье'), 'Московская область')
        self.assertIn('region=4593', sources.cian.build_url('Московская область', 1, 10_000_000))

    def test_refinements_preserve_rooms_but_explicit_city_change_clears_old_regions(self):
        base = merge_profile({'city':'Москва','cities':['Московская область'],'rooms_options':[1,2],'budget_max':10_000_000})
        change = explicit_search_fields({'rooms':2,'poi':['park']}, 'Парк рядом желателен', base)
        self.assertEqual(merge_profile(change, base)['rooms_options'], [1,2])
        self.assertEqual(merge_profile({'city':'Казань'},base)['cities'], [])
        self.assertEqual(explicit_search_fields({'city':'Москва'}, 'Только Москва', base)['cities'], [])
        no_region = explicit_search_fields({'city':'Московская область'}, 'Не Подмосковье', base)
        self.assertNotIn('city', no_region)
        self.assertEqual(no_region['cities'], [])

    async def test_all_variants_are_queried_and_deduplicated(self):
        p = {'city': 'Москва', 'cities': ['Московская область'], 'rooms': 1, 'rooms_options': [1, 2], 'budget_max': 10_000_000}
        got = []
        async def collect(q):
            got.append((q['city'], q['rooms']))
            return [listing()], [{'status': 'live', 'count': 1}]
        with patch.object(sources, '_collect_one', collect):
            items, reports = await sources.collect(p)
        self.assertEqual(set(got), {('Москва',1),('Москва',2),('Московская область',1),('Московская область',2)})
        self.assertEqual(len(items), 1)
        self.assertEqual(len(reports), 4)

    def test_central_one_room_can_beat_distant_two_room(self):
        profile = merge_profile({'city':'Самара','rooms_options':[1,2],'budget_max':10_000_000,'prefer_center':True})
        prefs = session.P.Prefs(); prefs.sync(profile)
        center = listing(id='center',city='Самара',rooms=1,price=8_500_000,lat=53.195,lng=50.11)
        outer = listing(id='outer',city='Самара',rooms=2,price=3_000_000,lat=53.27,lng=50.29)
        meta = {x['id']:{'score':50,'fair':{'fair':x['price'],'comps_count':8}} for x in (center,outer)}
        self.assertEqual(search_actions.ranked([outer,center],meta,prefs)[0]['id'],'center')
        self.assertLess(meta['center']['ranking']['center_km'],1)

    def test_better_relative_market_value_wins_for_equal_match(self):
        profile = {'city':'Казань'}
        match = {'checks':[]}
        cheap = value_rank(listing(price=7_000_000),profile,{'fair':8_000_000,'comps_count':8},match)
        dear = value_rank(listing(price=8_000_000),profile,{'fair':7_500_000,'comps_count':8},match)
        self.assertGreater(cheap['value_score'],dear['value_score'])
        self.assertGreater(cheap['discount_pct'],0)

    def test_no_discount_claim_with_sparse_comparables_or_fake_center(self):
        r = value_rank(listing(lat=None,lng=None),{'prefer_center':True},{'fair':10_000_000,'comps_count':1},{'checks':[]})
        self.assertIsNone(r['discount_pct'])
        self.assertIsNone(r['center_km'])

    def test_far_regional_comparables_do_not_prove_a_market_discount(self):
        x = listing(city='Московская область',price=4_000_000,lat=55.2,lng=38.2,district=None)
        pool = [listing(id=f'far{i}',city='Московская область',price=9_000_000,lat=56.2+i*.01,lng=37.2,district=None) for i in range(5)]
        fp = fair_price(x,pool)
        self.assertFalse(fp['value_supported'])
        self.assertIsNone(value_rank(x,{},fp,{'checks':[]})['discount_pct'])

    def test_fair_price_does_not_mix_moscow_and_region(self):
        x = listing(city='Москва',price=10_000_000)
        fp = fair_price(x,[listing(id='region',city='Московская область',price=3_000_000)])
        self.assertGreater(fp['fair'],9_000_000)
