"""Критерии и регрессии olga/dev: неизвестность, обязательность и порядок отбора."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend import db, geo, sources
from backend.agent import criteria as C, features as F, session
from backend.agent.actions import request as R, search as S
from backend.agent.profile import merge_profile
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


class CriteriaTest(unittest.TestCase):
    def test_missing_park_is_not_confirmed_by_school(self):
        p = {'poi': ['school', 'park']}
        result = C.evaluate(p, listing(nearby={'school': {'name': 'Школа', 'dist': 100}}))
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['unknown'], ['парк'])

    def test_unknown_and_failed_features_checked_independently(self):
        result = C.evaluate({'features': ['wardrobe', 'parking']}, listing(description='Нет паркинга.'))
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('парковка', result['failed'])
        self.assertIn('гардеробная', result['unknown'])

    def test_ready_to_move_excludes_unfinished_building(self):
        result = C.evaluate({'condition': 'ready'}, listing(condition='чистовая отделка', is_new=True, finished=False))
        self.assertEqual(result['status'], 'rejected')

    def test_each_house_and_area_constraint_checked(self):
        p = {'material': ['кирпич'], 'year_min': 2010, 'area_min': 60, 'kitchen_min': 12}
        result = C.evaluate(p, listing(material=None, year=1990, area=None, kitchen=8))
        self.assertIn('год постройки', result['failed'])
        self.assertIn('площадь кухни', result['failed'])
        self.assertIn('материал дома', result['unknown'])

    def test_advertising_claim_has_source_but_does_not_prove_distance(self):
        result = C.evaluate({'poi': ['park']}, listing(description='Рядом большой парк.'))
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['checks'][0]['source'], 'cian_description')
        self.assertIn('парк', result['checks'][0]['evidence'])

    def test_parking_does_not_imply_park(self):
        result = C.evaluate({'poi': ['park']}, listing(description='Удобная парковка.'))
        self.assertIsNone(result['checks'][0]['source'])

    def test_explicit_false_does_not_become_unknown_when_map_partially_failed(self):
        result = C.evaluate({'poi': ['school', 'park']}, listing(nearby={'school': None}))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['unknown'], ['парк'])

    def test_far_park_rejected_after_enrichment(self):
        p = {'poi': ['park']}
        self.assertEqual(C.evaluate(p, listing())['status'], 'unverified')
        self.assertEqual(C.evaluate(p, listing(nearby={'park': {'dist': 1400}}))['status'], 'rejected')

    def test_soft_failure_does_not_reject(self):
        p = {'poi': ['park'], 'soft_criteria': ['poi:park']}
        result = C.evaluate(p, listing(nearby={'park': None}))
        self.assertEqual(result['status'], 'confirmed')
        self.assertLess(result['preference_score'], 0)
        self.assertFalse(result['checks'][0]['required'])

    def test_loggia_balcony_and_both_remain_distinct(self):
        x = listing(balconies=1, loggias=1)
        self.assertTrue(F.feature_status(x, 'loggia'))
        self.assertFalse(F.feature_status(x, 'balcony_only'))
        self.assertFalse(F.feature_status(x, 'balcony_loggia'))
        self.assertTrue(F.feature_status(x, 'balcony'))

    def test_structured_false_is_not_overridden_by_description(self):
        self.assertFalse(F.feature_status(listing(separate_wc=False, description='Раздельный санузел'), 'separate_wc'))
        self.assertFalse(F.feature_status(listing(loggias=0, description='Есть лоджия'), 'loggia'))

    def test_seller_correction_takes_priority(self):
        result = C.evaluate({'features': ['parking']}, listing(description='Нет парковки', confirmed={'parking': True}))
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['checks'][0]['source'], 'seller_call')

    def test_walking_time_not_invented_from_transport_time(self):
        result = C.evaluate({'metro_max_minutes': 10}, listing(metro={'minutes': 4, 'mode': 'транспортом'}))
        self.assertEqual(result['status'], 'unverified')

    def test_neither_eight_answers_nor_any_values_required_for_search(self):
        p = merge_profile({'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000})
        self.assertEqual(C.evaluate(p, listing())['status'], 'confirmed')
        self.assertIsNone(p['condition'])
        self.assertIsNone(p['timeline'])


class SearchIntegrationTest(unittest.TestCase):
    def setUp(self):
        db.configure('sqlite://')
        geo._live_cache.clear()
        self.s = session.create_session('Квартира', ScriptedLLM())
        self.s.profile = merge_profile({'city': 'Казань', 'rooms': 2, 'budget_max': 9_000_000, 'payment': 'cash'})
        self.s.red_lines_ok = True
        self.s.prefs.sync(self.s.profile)
        self.s.ask = AsyncMock(return_value={'approved': True, 'data': {'action': 'proceed'}})

    def run_search(self, rows):
        async def go():
            with patch.object(sources, 'collect', AsyncMock(return_value=(rows, [{'source': 'Тест', 'count': len(rows), 'status': 'live'}]))), \
                 patch.object(sources, 'stored', return_value=[]), patch.object(sources, 'comparables', return_value=[]), \
                 patch.object(sources, 'more', AsyncMock(return_value=[])):
                result = await S.search_listings(self.s)
                await S.show_selection(self.s)
                return result
        return asyncio.run(go())

    def test_eleven_candidates_ranked_before_cutting_top_ten(self):
        rows = [listing(id=str(i), price=8_000_000) for i in range(10)] + [listing(id='best', price=6_000_000)]
        self.run_search(rows)
        self.assertEqual(len(self.s.shown), 10)
        self.assertIn('best', [x['id'] for x in self.s.shown])

    def test_complete_results_precede_unknown_and_rejected_not_shown(self):
        self.s.profile['features'] = ['parking']
        self.s.prefs.sync(self.s.profile)
        self.s.prefs.liked = ['bad']
        result = self.run_search([listing(id='missing', description=''), listing(id='good', description='Есть парковка'),
                                  listing(id='bad', description='Нет паркинга')])
        self.assertEqual([x['id'] for x in self.s.shown], ['good', 'missing'])
        self.assertEqual([x['match']['status'] for x in self.s.shown], ['confirmed', 'unverified'])
        self.assertEqual((result['confirmed'], result['unverified'], result['rejected']), (1, 1, 1))

    def test_map_results_filter_before_final_selection(self):
        self.s.profile['poi'] = ['park']
        self.s.prefs.sync(self.s.profile)
        rows = [listing(id='far', nearby={'park': {'name': 'Дальний парк', 'dist': 1400}}),
                listing(id='near', nearby={'park': {'name': 'Парк', 'dist': 100}})]
        self.run_search(rows)
        self.assertEqual([x['id'] for x in self.s.shown], ['near'])

    def test_soft_park_ranks_without_excluding_apartments(self):
        self.s.profile.update(poi=['park'], soft_criteria=['poi:park'])
        self.s.prefs.sync(self.s.profile)
        self.run_search([listing(id='absent', nearby={'park': None}), listing(id='near', nearby={'park': {'dist': 100}})])
        self.assertEqual([x['id'] for x in self.s.shown], ['near', 'absent'])
        self.assertIn('желательно', self.s.prefs.learned()[0])

    def test_removing_or_tightening_preferences_updates_selection(self):
        async def go():
            await R.update_request(self.s, not_first_floor=True, features=['loggia'], poi=['park'], soft_criteria=['poi:park'])
            self.s.search = {'shown_ids': {'old'}}
            self.s.agreed['old'] = 1
            self.s.chosen = 'old'
            await R.update_request(self.s, not_first_floor=False, features=[], poi=['park'], soft_criteria=[])
        asyncio.run(go())
        self.assertFalse(self.s.profile['not_first_floor'])
        self.assertEqual(self.s.profile['features'], [])
        self.assertEqual(self.s.profile['soft_criteria'], [])
        self.assertFalse(self.s.search['shown_ids'])
        self.assertFalse(self.s.agreed)
        self.assertIsNone(self.s.chosen)

    def test_relaxing_first_floor_reconsiders_original_listings(self):
        async def go():
            await R.update_request(self.s, not_first_floor=True, not_last_floor=True)
            rows = [listing(id='first',floor=1,floors=9,price=5_000_000),
                    listing(id='middle',floor=4,floors=9), listing(id='last',floor=9,floors=9)]
            with patch.object(sources, 'collect', AsyncMock(return_value=(rows, []))), patch.object(sources, 'stored', return_value=[]), \
                 patch.object(sources, 'comparables', return_value=[]), patch.object(sources, 'more', AsyncMock(return_value=[])):
                await S.search_listings(self.s)
                self.assertNotIn('first', [x['id'] for x in self.s.search['pool']])
                await R.update_request(self.s, not_first_floor=False)
                await S.show_selection(self.s)
            self.assertEqual([x['id'] for x in self.s.shown], ['first', 'middle'])
            self.assertTrue(self.s.profile['not_last_floor'])
        asyncio.run(go())

    def test_targeted_removal_clears_optional_park_and_preserves_other_constraints(self):
        async def go():
            await R.update_request(self.s, poi=['park','school'], soft_criteria=['poi:park'], features=['loggia'], not_first_floor=True, not_last_floor=True)
            await R.update_request(self.s, clear_criteria=['poi:park','not_first_floor'])
            self.assertEqual(self.s.profile['poi'], ['school'])
            self.assertNotIn('poi:park', self.s.profile['soft_criteria'])
            self.assertFalse(self.s.profile['not_first_floor'])
            self.assertTrue(self.s.profile['not_last_floor'])
            self.assertEqual(self.s.profile['features'], ['loggia'])
            await R.update_request(self.s, clear_criteria=['feature:loggia'])
            self.assertEqual(self.s.profile['features'], [])
        asyncio.run(go())

    def test_source_failure_is_not_presented_as_an_empty_market(self):
        async def go():
            self.s.search = {'report':[{'status':'unavailable'}]}
            await S.offer_search_alternatives(self.s)
            args = self.s.ask.call_args
            self.assertIn('не означает', args.args[2])
            self.assertIn('Повторить поиск', args.kwargs['chips'])
        asyncio.run(go())

    def test_new_client_turn_can_retry_map_after_expired_budget(self):
        self.s.profile['poi'] = ['park']
        self.s.prefs.sync(self.s.profile)
        self.s.search = {'map_deadline': 0, 'map_revision': 0}
        self.s.client_revision = 1
        async def go():
            with patch.object(geo, 'nearby', AsyncMock(return_value=False)) as query:
                await S.check_map(self.s, [listing()])
                self.assertGreater(query.call_args.kwargs['budget_s'], 0)
                self.assertEqual(self.s.search['map_revision'], 1)
        asyncio.run(go())

    def test_unknown_fields_can_be_explicitly_cleared(self):
        async def go():
            await R.update_request(self.s, year_min=2000, kitchen_min=12)
            await R.update_request(self.s, clear_criteria=['year_min', 'kitchen_min'])
        asyncio.run(go())
        self.assertIsNone(self.s.profile['year_min'])
        self.assertIsNone(self.s.profile['kitchen_min'])
