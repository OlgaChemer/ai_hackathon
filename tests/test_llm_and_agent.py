"""Проверки без сети: GigaChat-адаптер на моках httpx, рамки торга, экономика.

Запуск: .venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import json
import unittest

import httpx

from backend import economics as E
from backend.agent import data as D
from backend.agent import negotiation as N
from backend.agent import tools as T
from backend.llm import Tool, build_llm
from backend.llm.gigachat import GigaChatClient, to_gigachat_functions, to_gigachat_messages


def run(coro):
    return asyncio.run(coro)


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
                self.assertIn("RqUID", req.headers)
                return httpx.Response(200, json={"access_token": "tok", "expires_at": 4102444800000})
            calls["chat"] += 1
            body = json.loads(req.content)
            self.assertEqual(req.headers["Authorization"], "Bearer tok")
            self.assertEqual(body["functions"][0]["name"], "search")
            self.assertNotIn("tools", body)
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "",
                "function_call": {"name": "search", "arguments": {"rooms": 2}}}}]})

        c = self.make_client(handler)
        tool = Tool("search", "Поиск квартир", {"type": "object", "properties": {"rooms": {"type": "integer"}}})
        for _ in range(2):
            r = run(c.chat([{"role": "user", "content": "двушка"}], tools=[tool]))
            self.assertEqual(r.tool_calls[0].name, "search")
            self.assertEqual(r.tool_calls[0].arguments, {"rooms": 2})
        self.assertEqual(calls, {"oauth": 1, "chat": 2})

    def test_401_refreshes_token(self):
        state = {"oauth": 0, "chat": 0}

        def handler(req):
            if req.url.path.endswith("/oauth"):
                state["oauth"] += 1
                return httpx.Response(200, json={"access_token": f"t{state['oauth']}", "expires_at": 4102444800000})
            state["chat"] += 1
            if req.headers["Authorization"] == "Bearer t1":
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        r = run(self.make_client(handler).chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(r.text, "ok")
        self.assertEqual(state["oauth"], 2)

    def test_json_mode_adds_instruction(self):
        msgs = to_gigachat_messages([{"role": "system", "content": "Ты агент"}], json_mode=True)
        self.assertIn("JSON", msgs[0]["content"])
        self.assertEqual(to_gigachat_functions([Tool("a", "b", {})])[0], {"name": "a", "description": "b", "parameters": {}})


class NegotiationGuardsTest(unittest.TestCase):
    def test_offline_call_stays_within_client_limit(self):
        x = D.listing_by_id("kzn-101")
        fp = T.fair_price(x)
        n = N.Negotiation(listing=x, fair=fp, authority=min(9_000_000, fp["high"]))

        async def go():
            return [e async for e in N.run(build_llm("mock"), n)]

        events = run(go())
        end = events[-1]
        self.assertEqual(end["type"], "call_end")
        self.assertEqual(end["status"], "agreed")
        self.assertLessEqual(end["price"], n.authority)
        self.assertGreaterEqual(end["price"], x["seller"]["min_price"])
        offers = [e["offer"] for e in events if e["type"] == "turn" and e["speaker"] == "agent" and e["offer"]]
        self.assertEqual(offers, sorted(offers), "агент не должен откатывать предложение")
        self.assertIn("ассистент", events[1]["text"].lower())

    def test_buyer_guard_clamps_offer_above_limit(self):
        x = D.listing_by_id("kzn-101")
        n = N.Negotiation(listing=x, fair=T.fair_price(x), authority=8_800_000)
        move = {"utterance": "Предлагаю 8 950 000", "offer": 8_950_000, "accept": False}
        notes = N.guard_buyer(n, move, round_no=1)
        self.assertEqual(move["offer"], 8_800_000)
        self.assertTrue(notes)

    def test_seller_guard_respects_min_price(self):
        x = D.listing_by_id("kzn-101")
        n = N.Negotiation(listing=x, fair=T.fair_price(x), authority=8_980_000)
        move = {"utterance": "Ладно, 8 400 000", "ask": 8_400_000, "accept": False}
        N.guard_seller(n, move)
        self.assertEqual(move["ask"], x["seller"]["min_price"])


class ToolsAndEconomicsTest(unittest.TestCase):
    def test_parse_money(self):
        for text, value in [("до 9 млн", 9_000_000), ("8,7 млн", 8_700_000), ("8 700 000", 8_700_000), ("8 млн 650 тыс", 8_650_000)]:
            self.assertEqual(T.parse_money(text), value, text)

    def test_risky_listing_is_blocked(self):
        self.assertEqual(T.legal_check("kzn-102")["status"], "block")
        self.assertEqual(T.legal_check("kzn-101")["status"], "ok")

    def test_economics_matches_business_model(self):
        s = {x["key"]: x for x in E.scenarios()}
        self.assertEqual(s["pilot"]["contribution"], 101_300)
        self.assertAlmostEqual(s["pilot"]["roi"], 0.35, places=2)
        self.assertAlmostEqual(s["base"]["roi"], 6.6, places=1)


if __name__ == "__main__":
    unittest.main()
