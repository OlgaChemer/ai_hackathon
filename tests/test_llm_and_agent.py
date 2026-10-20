"""Проверки без сети: GigaChat-адаптер на моках httpx, разбор Циана, рамки торга,
финансовый помощник, обратная связь, экономика.

Запуск: .venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import json
import unittest

import httpx

from backend import economics as E
from backend.agent import finance as F
from backend.agent import negotiation as N
from backend.agent import preferences as P
from backend.agent import seller_sim
from backend.agent import tools as T
from backend.llm import Tool, build_llm
from backend.llm.gigachat import GigaChatClient, to_gigachat_functions, to_gigachat_messages
from backend.sources import cian


def run(coro):
    return asyncio.run(coro)


def listing(**kw):
    base = {
        "id": "cian-1", "source": "cian", "source_name": "Циан", "url": "https://kazan.cian.ru/sale/flat/1/",
        "city": "Казань", "district": "Приволжский", "address": "Ярышлар, 8", "rooms": 2, "area": 53.0,
        "kitchen": 10.6, "floor": 5, "floors": 19, "year": 2015, "condition": None,
        "material": "монолит", "price": 7_500_000, "days_on_market": 120,
        "metro": {"name": "Проспект Победы", "minutes": 12, "mode": "пешком"}, "photo": None,
        "description": "", "rosreestr_checked": True, "is_apartments": False,
        "is_auction": False, "seller_role": "собственник",
    }
    return {**base, **kw}


class GigaChatAdapterTest(unittest.TestCase):
    def make_client(self, handler):
        c = GigaChatClient(
            credentials="Y2xpZW50OnNlY3JldA==", scope="GIGACHAT_API_PERS", model="GigaChat-2-Max",
            auth_url="https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            base_url="https://gigachat.devices.sberbank.ru/api/v1",
        )
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://gigachat.devices.sberbank.ru/api/v1")
        return c

    def test_tools_become_functions_and_token_is_cached(self):
        calls = {"oauth": 0, "chat": 0}

        def handler(req: httpx.Request):
            if req.url.path.endswith("/oauth"):
                calls["oauth"] += 1
                self.assertTrue(req.headers["Authorization"].startswith("Basic "))
                return httpx.Response(200, json={"access_token": "tok", "expires_at": 4102444800000})
            calls["chat"] += 1
            body = json.loads(req.content)
            self.assertEqual(body["functions"][0]["name"], "search")
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "", "function_call": {"name": "search", "arguments": {"rooms": 2}}}}]})

        c = self.make_client(handler)
        tool = Tool("search", "Поиск квартир", {"type": "object", "properties": {"rooms": {"type": "integer"}}})
        for _ in range(2):
            r = run(c.chat([{"role": "user", "content": "двушка"}], tools=[tool]))
            self.assertEqual(r.tool_calls[0].arguments, {"rooms": 2})
        self.assertEqual(calls, {"oauth": 1, "chat": 2})

    def test_401_refreshes_token(self):
        state = {"oauth": 0}

        def handler(req):
            if req.url.path.endswith("/oauth"):
                state["oauth"] += 1
                return httpx.Response(200, json={"access_token": f"t{state['oauth']}", "expires_at": 4102444800000})
            if req.headers["Authorization"] == "Bearer t1":
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        self.assertEqual(run(self.make_client(handler).chat([{"role": "user", "content": "hi"}])).text, "ok")
        self.assertEqual(state["oauth"], 2)

    def test_json_mode_adds_instruction(self):
        msgs = to_gigachat_messages([{"role": "system", "content": "Ты агент"}], json_mode=True)
        self.assertIn("JSON", msgs[0]["content"])
        self.assertEqual(to_gigachat_functions([Tool("a", "b", {})])[0], {"name": "a", "description": "b", "parameters": {}})


class CianParsingTest(unittest.TestCase):
    PAGE = ('<title>Купить квартиру в Казани</title><script>x={"offers":[{"cianId":42,"totalArea":"51.2",'
            '"kitchenArea":"9.5","floorNumber":1,"roomsCount":2,"bargainTerms":{"priceRur":8900000},'
            '"building":{"floorsCount":9,"buildYear":2004},"repairType":"euro","creationDate":"2026-09-01T10:00:00",'
            '"fullUrl":"https://kazan.cian.ru/sale/flat/42/?context=x","phones":[{"number":"9000000000"}],'
            '"geo":{"address":[{"geoType":"street","name":"Чистопольская"},{"geoType":"house","name":"64"}],'
            '"districts":[{"type":"raion","name":"Ново-Савиновский"}],"undergrounds":[]}}]};</script>')

    def test_extract_and_normalize_without_phones(self):
        offers = cian.extract_offers(self.PAGE)
        x = cian.normalize(offers[0], "Казань")
        self.assertEqual(x["id"], "cian-42")
        self.assertEqual(x["address"], "Чистопольская, 64")
        self.assertEqual(x["condition"], "евроремонт")
        self.assertEqual(x["url"], "https://kazan.cian.ru/sale/flat/42/")
        self.assertNotIn("phones", x)
        self.assertNotIn("9000000000", json.dumps(x))

    def test_new_buildings_are_skipped(self):
        o = {"cianId": 1, "totalArea": "40", "bargainTerms": {"priceRur": 5e6, "saleType": "fz214"}}
        self.assertIsNone(cian.normalize(o, "Казань"))


class AgentToolsTest(unittest.TestCase):
    def test_parse_request_and_missing(self):
        p = T.merge_profile(T.parse_request_rules("ищу двушку, у нас ребёнок"))
        self.assertEqual(p["rooms"], 2)
        self.assertTrue(p["near_school"])
        self.assertEqual(T.missing_required(p), ["city", "budget_max"])
        p = T.merge_profile(T.parse_request_rules("Казань, до 9 млн"), p)
        self.assertEqual(T.missing_required(p), [])
        self.assertTrue(p["near_school"], "ответ на уточнение не должен стирать сказанное раньше")

    def test_fair_price_and_missing_info(self):
        pool = [listing(id=f"c{i}", price=7_000_000 + i * 100_000) for i in range(6)]
        fp = T.fair_price(listing(), pool)
        self.assertLess(fp["low"], fp["fair"])
        self.assertLess(fp["fair"], fp["high"])
        self.assertIn("condition", [q["key"] for q in T.missing_info(listing())])

    def test_legal_check_does_not_invent_risks(self):
        clean = T.legal_check(listing())
        self.assertEqual(clean["status"], "ok")
        flagged = T.legal_check(listing(description="Продаётся доля, квартира в наследстве"))
        self.assertEqual(flagged["status"], "warn")


class NegotiationTest(unittest.TestCase):
    def test_offline_call_has_three_phases_and_respects_limits(self):
        x = listing()
        fp = T.fair_price(x, [listing(id=f"c{i}", price=7_200_000 + i * 50_000) for i in range(6)])
        seller = seller_sim.build(x)
        n = N.Negotiation(listing=x, seller=seller, fair=fp, authority=min(9_000_000, x["price"], fp["high"]),
                          questions=T.missing_info(x), slots=T.client_slots())

        async def go():
            return [e async for e in N.run(build_llm("mock"), n)]

        events = run(go())
        phases = [e["phase"] for e in events if e["type"] == "call_phase"]
        self.assertEqual(phases, ["clarify", "bargain", "viewing"])
        end = events[-1]
        self.assertLessEqual(end["price"], n.authority)
        self.assertGreaterEqual(end["price"], seller["min_price"])
        self.assertTrue(end["learned"])
        self.assertIn(end["viewing"], n.slots)
        offers = [e["offer"] for e in events if e["type"] == "turn" and e["speaker"] == "agent" and e["offer"]]
        self.assertEqual(offers, sorted(offers), "агент не должен откатывать предложение")

    def test_guards(self):
        x = listing()
        n = N.Negotiation(listing=x, seller=seller_sim.build(x), fair=T.fair_price(x, [x]), authority=7_300_000)
        move = {"utterance": "Предлагаю 7 450 000", "offer": 7_450_000, "accept": False}
        N.guard_buyer(n, move, "bargain", first=False)
        self.assertEqual(move["offer"], 7_300_000)
        n.offer = 7_200_000
        smove = {"utterance": "Ладно, 7 100 000", "ask": 7_100_000, "accept": False}
        N.guard_seller(n, smove)
        self.assertGreaterEqual(smove["ask"], n.seller["min_price"])


class FinanceAndFeedbackTest(unittest.TestCase):
    def test_finance_plan(self):
        f = F.plan(9_000_000, {"monthly_income": 250_000, "monthly_expenses": 120_000, "savings": 2_000_000,
                               "horizon_months": 6, "risk": "moderate"})
        self.assertGreater(f["gap"], 0)
        self.assertEqual(f["months"], next(s for s in f["strategies"] if s["key"] == "moderate")["months"])
        self.assertAlmostEqual(sum(a["share"] for a in f["allocation"]), 1.0)
        aggressive = F.plan(9_000_000, {"risk": "aggressive", "horizon_months": 6})
        self.assertTrue(aggressive["risk_note"])

    def test_feedback_learning(self):
        a = listing(id="a", district="Приволжский", area=60, kitchen=12)
        b = listing(id="b", district="Кировский", area=44, kitchen=7, floor=1)
        c = listing(id="c", district="Кировский", area=46, kitchen=7)
        p = P.Prefs(liked=["a"], disliked=["b"])
        P.learn(p, {"a": a, "b": b, "c": c})
        self.assertNotIn("Кировский", p.districts_minus, "одного отказа мало, чтобы исключить район")
        p.disliked.append("c")
        P.learn(p, {"a": a, "b": b, "c": c})
        self.assertIn("Кировский", p.districts_minus)
        self.assertTrue(p.avoid_first)
        self.assertGreater(P.adjust(a, p), P.adjust(b, p))
        run(P.apply_text(p, "хочу кухню побольше, от 10 метров", build_llm("mock"), ["Приволжский"]))
        self.assertEqual(p.kitchen_min, 10.0)


class RequestDetailsTest(unittest.TestCase):
    def test_okrug_payment_and_budget_unknown(self):
        p = T.merge_profile(T.parse_request_rules("Найди трехкомнатную на юго-западе Москвы, рядом школа"))
        self.assertEqual((p["city"], p["rooms"], p["okrug"]), ("Москва", 3, "ЮЗАО"))
        p = T.merge_profile(T.parse_request_rules("не знаю бюджет, планирую ипотеку"), p)
        self.assertTrue(p["budget_unknown"])
        self.assertEqual(T.payment_from_text("покупаю за наличные"), "cash")
        self.assertEqual(T.payment_from_text("в ипотеку, но взнос ещё коплю"), "mortgage_saving")
        self.assertEqual(T.merge_profile({"city": "Казань", "okrug": "ЮЗАО"})["okrug"], None, "округа — только в Москве")

    def test_shares_are_skipped(self):
        o = {"cianId": 7, "totalArea": "60", "category": "flatShareSale", "shareAmount": "1/3",
             "bargainTerms": {"priceRur": 4_000_000}}
        self.assertIsNone(cian.normalize(o, "Москва"))
        self.assertIn("flat_share=2", cian.build_url("Москва", 3, 30_000_000, 1, "ЮЗАО"))
        self.assertIn("district%5B0%5D=10", cian.build_url("Москва", 3, 30_000_000, 1, "ЮЗАО"))

    def test_cash_deal_has_no_mortgage(self):
        plan = T.deal_plan(listing(), 7_300_000, None)
        self.assertTrue(plan["cash"])
        self.assertEqual(plan["loan"], 0)
        self.assertFalse(any("ипотек" in t["title"].lower() for t in plan["timeline"]))

    def test_affordability(self):
        a = F.affordable({"monthly_income": 250_000, "monthly_expenses": 120_000, "savings": 1_500_000,
                          "horizon_months": 12}, T.DEMO_RATE)
        self.assertGreater(a["budget"], 5_000_000)
        self.assertLessEqual(a["budget"], min(a["by_payment"], a["by_down"]) + 100_000)


class EconomicsTest(unittest.TestCase):
    def test_core_matches_business_model(self):
        s = {x["key"]: x for x in E.scenarios()}
        self.assertEqual(s["pilot"]["contribution"], 101_300)
        self.assertAlmostEqual(s["pilot"]["roi_core"], 0.35, places=2)
        self.assertAlmostEqual(s["base"]["roi_core"], 6.6, places=1)

    def test_finance_assistant_adds_revenue(self):
        on = E.unit(E.Inputs())
        off = E.unit(E.Inputs(fin_enabled=False))
        self.assertGreater(on["roi"], off["roi"])
        self.assertEqual(off["roi"], off["roi_core"])


if __name__ == "__main__":
    unittest.main()
