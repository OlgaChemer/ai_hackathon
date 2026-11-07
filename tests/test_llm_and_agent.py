"""Проверки без сети: GigaChat-адаптер на моках httpx, разбор Циана, рамки торга (поддельная модель звонка),
финансовый помощник, обратная связь, экономика.

Запуск: .venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from backend import db
from backend import economics as E
from backend import geo
from backend.agent import finance as F
from backend.agent import negotiation as N
from backend.agent import preferences as P
from backend.agent import deal as D
from backend.agent import features as FT
from backend.agent import mortgage as M
from backend.agent import pricing as PR
from backend.agent import profile as PF
from backend.agent import seller_sim
from backend.agent.actions.search import fits
from backend.llm import LLMError, Tool
from backend.llm.gigachat import GigaChatClient, to_gigachat_functions, to_gigachat_messages
from backend.sources import cian
from tests.fakes import NegotiationLLM

db.configure("sqlite://")  # база агента — в памяти: тесты не пишут в backend/data


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
                "role": "assistant", "content": "", "functions_state_id": "test-state",
                "function_call": {"name": "search", "arguments": {"rooms": 2}}}}]})

        c = self.make_client(handler)
        tool = Tool("search", "Поиск квартир", {"type": "object", "properties": {"rooms": {"type": "integer"}}})
        for _ in range(2):
            r = run(c.chat([{"role": "user", "content": "двушка"}], tools=[tool]))
            self.assertEqual(r.tool_calls[0].arguments, {"rooms": 2})
            self.assertEqual(r.functions_state_id, "test-state")
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

    def test_concurrent_dialogues_queue_instead_of_exceeding_generation_limit(self):
        active = 0
        peak = 0
        async def handler(req):
            nonlocal active, peak
            if req.url.path.endswith('/oauth'):
                return httpx.Response(200, json={'access_token': 'tok', 'expires_at': 4102444800000})
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        async def go():
            c = self.make_client(handler)
            try:
                return await asyncio.gather(*(c.chat([{'role': 'user', 'content': 'Тест'}]) for _ in range(4)))
            finally:
                await c._http.aclose()
        responses = run(go())
        self.assertEqual(peak, 1)
        self.assertEqual([r.text for r in responses], ['ok'] * 4)

    def test_json_mode_adds_instruction(self):
        msgs = to_gigachat_messages([{"role": "system", "content": "Ты агент"}], json_mode=True)
        self.assertIn("JSON", msgs[0]["content"])
        self.assertEqual(to_gigachat_functions([Tool("a", "b", {})])[0], {"name": "a", "description": "b", "parameters": {}})

    def test_native_json_schema_is_sent_to_gigachat(self):
        schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        def handler(req):
            if req.url.path.endswith('/oauth'):
                return httpx.Response(200,json={'access_token':'tok','expires_at':4102444800000})
            body=json.loads(req.content)
            self.assertEqual(body['response_format'], {'type':'json_schema','schema':schema,'strict':True})
            self.assertEqual(body['messages'][0]['content'], 'Проверь')
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})
        result=run(self.make_client(handler).chat_json([{'role':'system','content':'Проверь'}],json_schema=schema))
        self.assertEqual(result, {'ok':True})


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
    def test_bargaining_does_not_invent_financing_or_transaction_terms(self):
        x = listing(days_on_market=0)
        fp = {"overprice_pct": 0, "cheaper_comps": 0}
        self.assertEqual(PR.bargaining_arguments(x, fp), [])

    def test_profile_merge_and_missing(self):
        p = PF.merge_profile({"rooms": 2, "poi": ["school", "unknown"], "features": ["balcony", "balcony_loggia"]})
        self.assertEqual(PF.missing_required(p), ["city", "budget_max"])
        self.assertEqual((p["poi"], p["features"]), (["school"], ["balcony_loggia"]))  # неизвестное — прочь, «оба» включает «любой»
        p = PF.merge_profile({"city": "в Казани", "budget_max": 9_000_000, "rooms": None}, p)
        self.assertEqual((p["city"], p["budget_max"], p["rooms"]), ("Казань", 9_000_000, 2))  # пустое не стирает сказанное
        self.assertEqual(PF.missing_required(p), [])

    def test_fair_price_and_missing_info(self):
        pool = [listing(id=f"c{i}", price=7_000_000 + i * 100_000) for i in range(6)]
        fp = PR.fair_price(listing(), pool)
        self.assertLess(fp["low"], fp["fair"])
        self.assertLess(fp["fair"], fp["high"])
        self.assertIn("condition", [q["key"] for q in FT.missing_info(listing())])

    def test_legal_check_does_not_invent_risks(self):
        clean = D.legal_check(listing())
        self.assertEqual(clean["status"], "ok")
        flagged = D.legal_check(listing(description="Продаётся доля, квартира в наследстве"))
        self.assertEqual(flagged["status"], "warn")


class NegotiationTest(unittest.TestCase):
    def make(self, mode="auto", **kw):
        x = listing(**kw)
        fp = PR.fair_price(x, [listing(id=f"c{i}", price=7_200_000 + i * 50_000) for i in range(6)])
        return N.Negotiation(listing=x, seller=seller_sim.build(x), fair=fp, authority=min(9_000_000, x["price"], fp["high"]),
                             questions=FT.missing_info(x), slots=D.client_slots(), mode=mode)

    def test_call_has_three_phases_and_respects_limits(self):
        n = self.make()

        async def go():
            return [e async for e in N.run(NegotiationLLM(n), n)]

        events = run(go())
        self.assertEqual([e["phase"] for e in events if e["type"] == "call_phase"], ["clarify", "bargain", "viewing"])
        end = events[-1]
        self.assertLessEqual(end["price"], n.authority)
        self.assertGreaterEqual(end["price"], n.seller["min_price"])
        self.assertTrue(end["learned"])
        offers = [e["offer"] for e in events if e["type"] == "turn" and e["speaker"] == "agent" and e["offer"]]
        self.assertEqual(offers, sorted(offers), "агент не должен откатывать предложение")

    def test_phone_call_waits_for_seller_and_stops_on_hangup(self):
        n = self.make(mode="phone")
        answers = iter(["Ремонт свежий, документы готовы", N.HANGUP])

        async def seller_input():
            return next(answers, None)

        async def go():
            return [e async for e in N.run(NegotiationLLM(n), n, human_input=seller_input)]

        events = run(go())
        self.assertEqual(sum(e["type"] == "await_seller" for e in events), 3)  # уточнение, торг, просмотр
        said = [e["text"] for e in events if e["type"] == "turn" and e["speaker"] == "seller"]
        self.assertEqual(said[0], "Ремонт свежий, документы готовы")
        self.assertNotIn(N.HANGUP, said)
        self.assertEqual(events[-1]["status"], "no_deal")

    def test_model_failure_stops_the_call_honestly(self):
        n = self.make()

        class Down(NegotiationLLM):
            async def chat_json(self, *a, **k):
                raise LLMError("сеть")

        async def go():
            return [e async for e in N.run(Down(n), n)]

        with self.assertRaises(N.CallError):  # никаких сценарных подмен реплик
            run(go())

    def test_guards(self):
        x = listing()
        n = N.Negotiation(listing=x, seller=seller_sim.build(x), fair=PR.fair_price(x, [x]), authority=7_300_000)
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
        p.sync({"kitchen_min": 10.0, "features": ["wardrobe"], "avoid_districts": ["Советский"]})  # пожелания от агента
        self.assertEqual((p.kitchen_min, p.features), (10.0, ["wardrobe"]))
        self.assertTrue({"Кировский", "Советский"} <= p.districts_minus, "выученное из отметок не теряется")


class RequestDetailsTest(unittest.TestCase):
    def test_okrug_only_in_moscow(self):
        p = PF.merge_profile({"city": "Москва", "rooms": 3, "okrug": "юго-запад", "payment": "наличные"})
        self.assertEqual((p["city"], p["okrug"], p["payment"]), ("Москва", "ЮЗАО", None))  # неизвестный способ оплаты — нет
        self.assertEqual(PF.merge_profile({"city": "Казань", "okrug": "ЮЗАО"})["okrug"], None, "округа — только в Москве")

    def test_shares_are_skipped(self):
        o = {"cianId": 7, "totalArea": "60", "category": "flatShareSale", "shareAmount": "1/3",
             "bargainTerms": {"priceRur": 4_000_000}}
        self.assertIsNone(cian.normalize(o, "Москва"))
        self.assertIn("flat_share=2", cian.build_url("Москва", 3, 30_000_000, 1, "ЮЗАО"))
        self.assertIn("district%5B0%5D=10", cian.build_url("Москва", 3, 30_000_000, 1, "ЮЗАО"))

    def test_cash_deal_has_no_mortgage(self):
        plan = D.deal_plan(listing(), 7_300_000, None)
        self.assertTrue(plan["cash"])
        self.assertEqual(plan["loan"], 0)
        self.assertFalse(any("ипотек" in t["title"].lower() for t in plan["timeline"]))

    def test_affordability(self):
        a = F.affordable({"monthly_income": 250_000, "monthly_expenses": 120_000, "savings": 1_500_000,
                          "horizon_months": 12}, M.DEMO_RATE)
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


class WishesAndMapTest(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        from backend.config import settings
        no_catalog = patch.object(geo, "settings", replace(settings, twogis_api_key=""))
        no_catalog.start()
        self.addCleanup(no_catalog.stop)

    def test_features_from_description(self):
        x = listing(description="Есть гардеробная, лоджия застеклена")
        self.assertTrue(FT.feature_status(x, "wardrobe"))
        self.assertIsNone(FT.feature_status(listing(description="Хорошая квартира"), "wardrobe"))
        self.assertFalse(FT.feature_status(listing(description="Квартира без балкона"), "balcony"))
        asked = FT.missing_info(listing(description=""), 4, ["wardrobe"])
        self.assertEqual(asked[0]["label"], "гардеробная")  # агент спросит у продавца

    def test_named_district_matches_any_case(self):
        self.assertTrue(PF.in_districts({"district": "Черёмушки"}, ["черемушек"]))
        self.assertFalse(PF.in_districts({"district": "Ясенево"}, ["Черёмушки"]))

    def test_nearby_from_map(self):
        # ответ Overpass: школа (центр здания) и река (контур)
        elements = [
            {"type": "way", "id": 1, "tags": {"amenity": "school", "name": "Школа № 1"}, "center": {"lat": 55.7905, "lon": 49.1205}},
            {"type": "way", "id": 2, "tags": {"waterway": "river", "name": "Казанка"},
             "geometry": [{"lat": 55.800, "lon": 49.100}, {"lat": 55.800, "lon": 49.140}]},
        ]
        queries = []

        async def fake_overpass(query, timeout):
            queries.append(query)
            return elements

        x = listing(id="m1", lat=55.7900, lng=49.1200)   # ~1,1 км от реки, школа в 70 м
        near_river = listing(id="m2", lat=55.7980, lng=49.1200)
        geo._live_cache.clear()
        with patch.object(geo, "_overpass", fake_overpass):
            self.assertTrue(run(geo.nearby([x, near_river], ["school", "water"])))
            self.assertTrue(run(geo.nearby([x], ["school"])))  # повтор — из кэша
        self.assertEqual(len(queries), 1)  # один запрос на обе квартиры и оба вида
        self.assertEqual(x["nearby"]["school"]["name"], "Школа № 1")
        self.assertLess(x["nearby"]["school"]["dist"], 100)
        self.assertIsNone(x["nearby"]["water"])                       # реки в 1 км нет
        self.assertLess(near_river["nearby"]["water"]["dist"], 250)  # до берега, а не до центра реки
        self.assertFalse(geo.has_all(x, ["school", "water"]))

    def test_balcony_and_loggia_means_both(self):
        self.assertEqual(FT.merge_features(["balcony"], ["balcony_loggia"]), ["balcony_loggia"])
        both = listing(balconies=2, loggias=1)
        only_loggia = listing(balconies=1, loggias=1)
        by_text = listing(description="Застеклённый балкон и лоджия")
        self.assertEqual([FT.feature_status(x, "balcony_loggia") for x in (both, only_loggia, by_text)], [True, False, True])

    def test_selection_keeps_only_confirmed_wishes(self):
        prefs = P.Prefs(features=["wardrobe"], poi=["school"])
        good = listing(description="Есть гардеробная", nearby={"school": {"name": "Школа", "dist": 300}})
        no_school = listing(description="Есть гардеробная", nearby={"school": None})
        unknown = listing(description="Хорошая квартира", nearby={"school": {"name": "Школа", "dist": 300}})
        self.assertEqual([fits(x, prefs) for x in (good, no_school, unknown)], [True, False, False])
        self.assertFalse(fits(no_school, prefs))  # общая ошибка карты не отменяет уже известное отсутствие школы

    def test_seller_reply_with_price_skips_llm(self):
        x = listing()
        n = N.Negotiation(listing=x, seller=seller_sim.build(x), fair=PR.fair_price(x, [x]), authority=7_300_000)
        n.offer = 7_200_000

        class Boom:
            name = "deepseek"

            async def chat_json(self, *a, **k):
                raise AssertionError("модель не нужна")

        move = run(N.parse_human_seller(Boom(), n, "Меньше 7 500 000 не отдам"))
        self.assertEqual(move["ask"], 7_500_000)
        self.assertFalse(move["accept"])
        self.assertTrue(run(N.parse_human_seller(Boom(), n, "Ладно, согласен"))["accept"])

    def test_counter_price_without_mln_is_not_agreement(self):
        """Агент: 23,4 млн. Продавец: «Нет, давай 23 600» — это встречная цена, а не согласие на 23,4."""
        x = listing(price=23_650_000)
        n = N.Negotiation(listing=x, seller=seller_sim.build(x), fair=PR.fair_price(x, [x]), authority=23_650_000)
        n.offer = 23_400_000

        class Boom:
            name = "deepseek"

            async def chat_json(self, *a, **k):
                raise AssertionError("модель не нужна")

        move = run(N.parse_human_seller(Boom(), n, "Нет, давай 23 600."))
        self.assertEqual((move["ask"], move["accept"]), (23_600_000, False))
        self.assertEqual(run(N.parse_human_seller(Boom(), n, "Хорошо, 23,4 — по рукам"))["accept"], True)
        # агент «принимает», но вслух называет не ту цену — реплику исправляем
        n.asking = 23_600_000
        agent = {"utterance": "Хорошо, фиксирую 23 400 000 ₽.", "offer": None, "accept": True}
        N.guard_buyer(n, agent, "bargain", first=False)
        self.assertIn("23 600 000", agent["utterance"])


if __name__ == "__main__":
    unittest.main()
