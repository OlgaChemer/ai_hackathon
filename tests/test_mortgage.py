"""Ипотечные программы: семейная ипотека с 01.10.2026, рыночная ставка, маткапитал, новостройки,
и шаг «Финансы» конвейера целиком (без сети, модель — офлайн-заглушка)."""
from __future__ import annotations

import asyncio
import json
import unittest

from backend import db
from backend.agent import deal as D
from backend.agent import finance as F
from backend.agent import mortgage as M
from backend.agent import profile as PF
from backend.agent import session as O
from tests.fakes import ScriptedLLM, call, drive, say


def by_key(r):
    return {c["key"]: c for c in r["programs"]}


class ProgramsTest(unittest.TestCase):
    def test_kazan_secondary_family_goes_to_new_builds(self):
        """Казань, ребёнок 4 года, вторичка: семейная не проходит, считаем по рыночной, предлагаем новостройку."""
        r = M.programs(9_000_000, "Казань", {"children": 1, "youngest_age": 4, "matcap": True})
        p = by_key(r)
        self.assertEqual(r["chosen"], "market")
        self.assertEqual(p["family"]["status"], "no")
        self.assertIn("строится", p["family"]["main_reason"])
        self.assertEqual(p["family_new"]["rate"], 0.10)              # 1 ребёнок, регион
        self.assertEqual(p["family_new"]["down_payment"], 3_000_000)  # лимит 6 млн -> взнос 3 млн
        self.assertLess(p["family_new"]["monthly"], p["market"]["monthly"])
        self.assertEqual(r["matcap"]["own_down"], 1_800_000 - M.MATCAP_FIRST)
        self.assertFalse(p["family"]["show_payment"])
        self.assertIn("не подходит", r["explanation"])

    def test_scale_by_children_and_region(self):
        self.assertEqual(M.family_terms("Казань", 1), (0.10, 6_000_000))
        self.assertEqual(M.family_terms("Казань", 3), (0.06, 10_000_000))
        self.assertEqual(M.family_terms("Москва", 1), (0.12, 12_000_000))
        self.assertEqual(M.family_terms("Москва", 7), (0.04, 18_000_000))

    def test_family_ok_in_small_town(self):
        r = M.programs(7_000_000, "Альметьевск", {"children": 2, "youngest_age": 2}, secondary_ok=True)
        p = by_key(r)
        self.assertEqual((r["chosen"], r["rate"]), ("family", 0.08))
        self.assertGreater(p["family"]["monthly_after_subsidy"], p["family"]["monthly"])

    def test_child_too_old_or_unknown(self):
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 2, "youngest_age": 9}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 0}))["family"]["status"], "no")
        self.assertEqual(by_key(M.programs(8_000_000, "Казань", {"children": 1}))["family"]["status"], "check")

    def test_limit_blocks_even_where_secondary_allowed(self):
        r = M.programs(9_000_000, "Альметьевск", {"children": 1, "youngest_age": 1}, secondary_ok=True)
        self.assertEqual((by_key(r)["family"]["status"], by_key(r)["family"]["needs_down"], r["chosen"]), ("no", 3_000_000, "market"))

    def test_new_build_passes_family(self):
        r = M.programs(7_000_000, "Казань", {"children": 1, "youngest_age": 4}, housing="new", is_secondary=False)
        self.assertEqual(r["chosen"], "family")
        self.assertIn("Считаю для новостройки", r["explanation"])
        pay = M.listing_payment({"price": 7_000_000, "city": "Казань", "is_new": True}, {"children": 1, "youngest_age": 4})
        self.assertEqual((pay["program"], pay["rate"]), ("семейная", 0.10))

    def test_market_rate_depends_on_down_payment(self):
        self.assertAlmostEqual(M.market_rate(0.2), 0.175)
        self.assertAlmostEqual(M.market_rate(0.5), 0.155)
        self.assertAlmostEqual(M.DEMO_RATE, 0.175)


class CalculationsTest(unittest.TestCase):
    def test_matcap_reduces_savings_need(self):
        a, b = F.plan(9_000_000, {}), F.plan(9_000_000, {}, matcap=M.MATCAP_FIRST)
        self.assertEqual(a["need"] - b["need"], M.MATCAP_FIRST)
        self.assertEqual(a["down_payment"], b["down_payment"], "кредит считается от полного взноса")

    def test_scenarios_and_deal_use_program_rate(self):
        m = M.mortgage_scenarios(7_000_000, 200_000, rate=0.08, program="Семейная ипотека")
        self.assertTrue(all(s["rate"] == 0.08 for s in m["scenarios"]))
        self.assertLess(m["scenarios"][0]["monthly"], M.mortgage_scenarios(7_000_000, 200_000)["scenarios"][0]["monthly"])
        lst = {"price": 7_200_000}
        d_fam = D.deal_plan(lst, 7_000_000, 1_400_000, rate=0.08, program="Семейная ипотека")
        self.assertLess(d_fam["monthly"], D.deal_plan(lst, 7_000_000, 1_400_000)["monthly"])
        self.assertIn("Семейная ипотека", d_fam["timeline"][1]["note"])

    def test_deal_plan_kinds(self):
        new = D.deal_plan({"price": 7_000_000, "is_new": True}, 6_900_000, 1_400_000)
        self.assertTrue(new["new"] and not new["assignment"])
        self.assertTrue(any("Эскроу" in s["title"] for s in new["timeline"]))
        assign = D.deal_plan({"price": 7_000_000, "is_new": True, "sale_type": "договор уступки"}, 6_900_000, 1_400_000)
        self.assertTrue(assign["assignment"])
        cash = D.deal_plan({"price": 7_000_000}, 6_900_000, None)
        self.assertEqual((cash["cash"], cash["monthly"], cash["down_payment"]), (True, 0, 6_900_000))

    def test_zero_children_and_studio_survive_merge(self):
        self.assertEqual(PF.merge_profile({"city": "Казань", "children": 0})["children"], 0)
        self.assertEqual(PF.merge_profile({"rooms": 0})["rooms"], 0)
        self.assertIs(PF.merge_profile({"matcap": False})["matcap"], False)  # «маткапитала нет» — не переспрашиваем


class FinanceActionsTest(unittest.TestCase):
    """Шаг «Финансы» через действия агента: модель (по сценарию) вызывает действия, клиент отвечает на карточки."""

    def setUp(self):
        db.configure("sqlite://")

    def agent(self, *steps):
        s = O.create_session("Двушка в Казани до 9 млн", ScriptedLLM(*steps))
        s.profile = PF.merge_profile({"city": "Казань", "rooms": 2, "budget_max": 9_000_000})
        s.red_lines_ok = True
        return s

    def test_family_matcap_and_savings_plan(self):
        s = self.agent(call("ask_payment"), call("ask_family"), call("mortgage_programs"), call("savings_plan"), say("Готово."))
        events = asyncio.run(drive(s, {"payment": (True, {"payment": "mortgage_saving"}),
                                       "family": (True, {"children": 1, "youngest_age": 4, "matcap": M.MATCAP_FIRST})}))
        finance = [e["data"] for e in events if e["type"] == "result" and e["kind"] == "finance"][-1]
        self.assertEqual(finance["mode"], "saving")
        self.assertEqual(finance["programs"]["chosen"], "market")  # Казань, вторичка
        self.assertEqual(finance["finance"]["matcap"], M.MATCAP_FIRST)
        self.assertTrue(any("Семейная ипотека здесь не подходит" in e.get("text", "") for e in events))
        self.assertEqual(s.profile["payment"], "mortgage_saving")

    def test_order_is_enforced_by_code(self):
        """Модель торопится: ипотека без семьи и поиск без финансов не выполняются — она получает объяснение."""
        s = self.agent(call("mortgage_programs"), call("search_listings"), say("Сначала уточню семью."))
        s.profile["payment"] = "mortgage_ready"
        asyncio.run(drive(s))
        results = [json.loads(m["content"]) for m in s.messages if m["role"] == "tool"]
        self.assertIn("ask_family", results[0]["error"])
        self.assertIn("mortgage_programs", results[1]["error"])

    def test_cash_needs_no_programs(self):
        s = self.agent(call("ask_payment"), call("mortgage_programs"), say("Ипотека не нужна."))
        events = asyncio.run(drive(s, {"payment": (True, {"payment": "cash"})}))
        self.assertEqual([e["data"]["mode"] for e in events if e["type"] == "result" and e["kind"] == "finance"], ["cash"])


class QuestionsTest(unittest.TestCase):
    def test_questions_vs_new_conditions(self):
        from backend.agent import qa
        self.assertTrue(qa.is_question("Сколько я переплачу?"))
        self.assertTrue(qa.is_question("что такое СБР"))
        self.assertFalse(qa.is_question("а если трёшку?"))  # новое условие поиска, а не вопрос
        self.assertFalse(qa.is_question("можно с балконом?"))


if __name__ == "__main__":
    unittest.main()
