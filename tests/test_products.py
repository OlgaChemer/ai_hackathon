"""Финансовые продукты Сбера по трём цепочкам оплаты: наличные, ипотека со взносом, ипотека без взноса."""
import copy
import json
import unittest
from unittest.mock import AsyncMock

from backend import db
from backend.agent import actions, capabilities, products as PR, session
from backend.agent.actions.request import apply_request
from backend.agent.profile import merge_profile
from backend.calculation_policy import POLICY, validate_products
from backend.llm import ToolCall
from tests.fakes import ScriptedLLM
from tests.test_llm_and_agent import listing


def answer(approved, **data):
    return AsyncMock(return_value={'approved': approved, 'data': data})


class ProductsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')

    def agent(self, **profile):
        s = session.create_session('', ScriptedLLM())
        s.profile = merge_profile({'city': 'Казань', 'rooms': 2, 'budget_max': 15_000_000, 'payment': 'mortgage_ready',
                                   'children': 0, 'deposit': 3_000_000, 'housing': 'secondary', **profile})
        s.profile['_approved_budget_max'] = s.profile['budget_max']
        s.red_lines_ok = True
        s.prefs.sync(s.profile)
        return s

    def chosen(self, s, price=12_000_000, **fields):
        x = listing(**{'city': 'Казань', 'price': price, 'is_new': False, **fields})
        s.search = {'listings': [x], 'pool': [x], 'shown_ids': {x['id']}, 'meta': {}, 'comps': [x]}
        s.shown = [x]
        s.calls = {x['id']: {'status': 'agreed', 'price': price, 'viewing': 'завтра', 'slots': ['завтра'], 'learned': {}}}
        s.agreed = {x['id']: price}
        s.chosen = x['id']
        return x

    async def act(self, s, name, ask=None, **args):
        if ask is not None:
            s.ask = ask
        result = await actions.execute(s, ToolCall(name, args, name))
        self.assertNotIn('error', result, result)
        return result

    async def finance(self, s):
        await self.act(s, 'mortgage_programs')
        if s.profile['payment'] == 'mortgage_saving':
            await self.act(s, 'savings_plan', ask=answer(True))

    # ---------------------------------------------------------- карточка «Деньги к сделке»

    async def test_cash_buyer_gets_parking_and_title_without_life_insurance(self):
        s = self.agent(payment='cash', deposit=None)
        x = self.chosen(s)
        o = PR.offer(x, 12_000_000, s.profile)
        self.assertEqual(o['chain'], 'cash')
        self.assertEqual([i['key'] for i in o['items']], ['parking', 'title'])
        days = POLICY['products']['days_to_settlement']['cash']
        self.assertEqual(o['parking']['income'], round(12_000_000 * 0.145 * days / 365, -2))
        new = PR.offer({**x, 'is_new': True}, 12_000_000, s.profile)
        self.assertEqual([i['key'] for i in new['items']], ['parking'])

    async def test_market_mortgage_shows_insurance_against_rate_surcharge_and_salary_discount(self):
        s = self.agent()
        x = self.chosen(s)
        o = PR.offer(x, 12_000_000, s.profile)
        self.assertEqual(o['chain'], 'mortgage')
        self.assertFalse(o['preferential'])
        self.assertTrue(o['salary_available'])
        v = o['variants']
        self.assertAlmostEqual(v['00']['rate'] - v['10']['rate'], POLICY['products']['life_refusal_surcharge'])
        self.assertAlmostEqual(v['10']['rate'] - v['11']['rate'], POLICY['products']['salary_discount'])
        self.assertGreater(v['00']['monthly'], v['10']['monthly'])
        self.assertGreater(v['10']['monthly'], v['11']['monthly'])
        life = next(i for i in o['items'] if i['key'] == 'life')
        self.assertFalse(life['locked'])
        self.assertLess(life['value'], (v['00']['monthly'] - v['10']['monthly']) * 12,
                        'Полис должен окупаться снижением ставки — иначе рекомендация нечестная')
        property_ = next(i for i in o['items'] if i['key'] == 'property')
        self.assertTrue(property_['locked'])

    async def test_family_mortgage_has_fixed_rate_and_mandatory_life_insurance(self):
        s = self.agent(children=1, youngest_age=3, matcap=0, housing='new')
        x = self.chosen(s, price=7_000_000, is_new=True)
        o = PR.offer(x, 7_000_000, s.profile)
        self.assertTrue(o['preferential'])
        self.assertFalse(o['salary_available'])
        self.assertEqual({row['rate'] for row in o['variants'].values()}, {o['base_rate']})
        choice = PR.choose(o, False, {})
        self.assertTrue(choice['picked']['life'], 'Для льготной ставки страхование жизни не снимается')
        package = PR.package(o, choice)
        self.assertEqual(package['monthly_saving'], 0)
        self.assertIn('одинаковая во всех банках', package['rows'][0]['text'])

    async def test_offer_is_shown_once_and_declining_raises_rate_in_the_deal_plan(self):
        s = self.agent()
        await self.finance(s)
        x = self.chosen(s)
        self.assertFalse(capabilities.decision(s, 'prepare_deal')[0])
        self.assertTrue(capabilities.decision(s, 'offer_products')[0])
        result = await self.act(s, 'offer_products', ask=answer(False), listing_id=x['id'])
        self.assertEqual(result['picked'], [])
        self.assertFalse(capabilities.decision(s, 'offer_products')[0], 'Предложение не повторяется')
        self.assertTrue(capabilities.decision(s, 'prepare_deal')[0])
        o = s.products['offer']
        self.assertEqual(s.products['choice']['rate'], o['variants']['00']['rate'])
        await self.act(s, 'prepare_deal', ask=answer(False), listing_id=x['id'])
        plan = next(e['data'] for e in s.history if e.get('kind') == 'deal')
        self.assertEqual(plan['rate'], o['variants']['00']['rate'])
        self.assertIn('Страхование квартиры в залоге, 1-й год', plan['costs'])
        self.assertNotIn('Страхование жизни, 1-й год', plan['costs'])
        self.assertIsNone(plan['package']['headline'], 'Без пакета нечем хвалиться')

    async def test_accepted_package_lowers_payment_and_shows_one_headline(self):
        s = self.agent()
        await self.finance(s)
        x = self.chosen(s)
        await self.act(s, 'offer_products', ask=answer(True, life=True, property=True, parking=True, salary=True),
                       listing_id=x['id'])
        o, choice = s.products['offer'], s.products['choice']
        self.assertEqual(choice['rate'], o['variants']['11']['rate'])
        await self.act(s, 'prepare_deal', ask=answer(False), listing_id=x['id'])
        plan = next(e['data'] for e in s.history if e.get('kind') == 'deal')
        self.assertEqual(plan['monthly'], o['variants']['11']['monthly'])
        package = plan['package']
        self.assertEqual(package['monthly_saving'], o['variants']['00']['monthly'] - o['variants']['11']['monthly'])
        self.assertIn('Условия Сбера снижают платёж', package['headline'])
        self.assertEqual(plan['costs']['Страхование жизни, 1-й год'], o['life_cost'])
        self.assertTrue(any(e.get('text', '').startswith('Условия Сбера') for e in s.history if e['type'] == 'message'))

    async def test_unknown_deposit_after_choice_asks_for_it_instead_of_dead_end(self):
        s = self.agent(deposit=None)
        s.profile['_deposit_skipped'] = True
        self.chosen(s)
        self.assertTrue(capabilities.decision(s, 'ask_deposit')[0])
        self.assertFalse(capabilities.decision(s, 'offer_products')[0])

    async def test_changed_conditions_drop_the_package(self):
        s = self.agent()
        await self.finance(s)
        x = self.chosen(s)
        await self.act(s, 'offer_products', ask=answer(True), listing_id=x['id'])
        apply_request(s, {'city': 'Москва'})
        self.assertEqual(s.products, {})

    # ---------------------------------------------------------- развилка до звонков

    async def saving_agent(self, price):
        s = self.agent(payment='mortgage_saving', deposit=1_000_000, budget_max=price)
        await self.finance(s)
        x = listing(city='Казань', price=price, is_new=False)
        s.search = {'listings': [x], 'pool': [x], 'shown_ids': {x['id']}, 'meta': {}, 'comps': [x]}
        s.shown = [x]
        s.call_targets = [x['id']]
        s.request_intent = 'call'
        return s, x

    async def test_saving_client_is_asked_before_calls_and_save_means_no_calls(self):
        s, x = await self.saving_agent(10_000_000)
        fork = PR.fork(s)
        self.assertFalse(fork['now']['ok'], 'Из подушки 20% взноса за 10 млн не собрать')
        self.assertEqual(fork['recommended'], 'save')
        self.assertFalse(capabilities.decision(s, 'call_seller')[0])
        self.assertTrue(capabilities.decision(s, 'financing_choice')[0])
        from backend.agent.workflow import next_search_action
        self.assertEqual(next_search_action(s), 'financing_choice')
        result = await self.act(s, 'financing_choice', ask=answer(True, choice='save'))
        self.assertFalse(result['calls_allowed'])
        self.assertEqual(s.call_targets, [])
        self.assertFalse(capabilities.decision(s, 'call_seller')[0])
        self.assertIn('deposit', s.opened)
        # Клиент вернулся и снова просит звонить — развилка показывается заново, по текущим ценам.
        s.call_targets = [x['id']]
        self.assertTrue(capabilities.decision(s, 'financing_choice')[0])

    async def test_buy_now_from_cushion_unlocks_calls_for_the_same_listings(self):
        s, x = await self.saving_agent(6_000_000)
        fork = PR.fork(s)
        self.assertTrue(fork['now']['ok'])
        self.assertGreater(fork['gap_max'], 0)
        self.assertLess(fork['now']['cushion_left'], fork['now']['cushion'])
        await self.act(s, 'financing_choice', ask=answer(True, choice='now'))
        self.assertEqual(s.call_targets, [x['id']])
        self.assertTrue(PR.fork_resolved(s))
        self.assertFalse(capabilities.decision(s, 'financing_choice')[0])
        other = listing(id='other', city='Казань', price=6_000_000, is_new=False)
        s.search['listings'].append(other)
        s.shown.append(other)
        s.call_targets = [x['id'], 'other']
        self.assertFalse(PR.fork_resolved(s), 'Решение касалось только прежних квартир')

    async def test_no_fork_when_money_is_enough_or_client_is_not_saving(self):
        s, x = await self.saving_agent(4_000_000)
        self.assertIsNone(PR.fork(s))
        self.assertTrue(PR.fork_resolved(s))
        self.assertFalse(capabilities.decision(s, 'financing_choice')[0])
        ready = self.agent()
        ready.call_targets = ['any']
        self.assertTrue(PR.fork_resolved(ready))

    async def test_fork_card_payload_is_serialisable_and_card_answers_are_validated(self):
        s, x = await self.saving_agent(10_000_000)
        json.dumps(PR.fork(s))
        from backend.agent import validation
        validation.confirmation('financing', {'choice': 'save'})
        validation.confirmation('products', {'life': True, 'salary': False, 'parking': True})
        with self.assertRaises(ValueError):
            validation.confirmation('financing', {'choice': 'maybe'})


class ProductsPolicyTest(unittest.TestCase):
    def test_discounts_stay_close_to_market(self):
        bad = copy.deepcopy(POLICY)
        bad['products']['salary_discount'] = 0.05
        with self.assertRaises(ValueError):
            validate_products(bad)
        bad = copy.deepcopy(POLICY)
        bad['products']['sources'] = []
        with self.assertRaises(ValueError):
            validate_products(bad)
        validate_products(POLICY)


if __name__ == '__main__':
    unittest.main()
