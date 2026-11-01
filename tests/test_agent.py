"""Цикл агента: модель выбирает действия, код выполняет и держит рамки. Модель — поддельная, по сценарию."""
from __future__ import annotations

import asyncio
import json
import unittest

from backend import db
from backend.agent import actions, loop
from backend.agent import session as O
from backend.llm.gigachat import to_gigachat_messages
from tests.fakes import ScriptedLLM, call, drive, last_result, say


def tool_results(s) -> list[dict]:
    return [json.loads(m["content"]) for m in s.messages if m["role"] == "tool"]


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")

    def test_actions_are_described_for_the_model(self):
        tools = actions.tools()
        self.assertGreaterEqual(len(tools), 15)
        for t in tools:
            self.assertTrue(t.description and t.parameters["type"] == "object", t.name)
            json.dumps(t.parameters)  # схема уходит модели как JSON

    def test_model_reads_results_and_order_is_enforced(self):
        def after_search_refused(messages):
            self.assertIn("confirm_red_lines", last_result(messages)["error"])  # модель видит, что сделать сначала
            return say("Сначала подтвердим рамки.")
        s = O.create_session("двушка в Казани до 9 млн", ScriptedLLM(
            call("update_request", city="Казань", rooms=2, budget_max=9_000_000), call("search_listings"), after_search_refused))
        asyncio.run(drive(s))
        profile, refused = tool_results(s)
        self.assertEqual(profile["missing"], [])
        self.assertIn("error", refused)
        self.assertEqual(s.messages[-1], {"role": "assistant", "content": "Сначала подтвердим рамки."})

    def test_unknown_action_and_bad_arguments_do_not_crash(self):
        s = O.create_session("x", ScriptedLLM(call("fly_to_mars"), call("probe_budget"), say("Ок.")))
        asyncio.run(drive(s))
        unknown, bad = tool_results(s)
        self.assertIn("Нет действия", unknown["error"])
        self.assertIn("error", bad)

    def test_model_down_is_said_honestly_and_retried(self):
        model = ScriptedLLM(say("Снова на связи."), fail=True)
        s = O.create_session("привет", model)

        async def go():
            events = await drive(s, until=lambda ev: ev["type"] == "message" and "не отвечает" in ev.get("text", ""))
            model.fail = False
            s.inbox.put_nowait("попробуй ещё раз")
            return events + await drive(s)
        texts = [e.get("text", "") for e in asyncio.run(go()) if e["type"] == "message"]
        self.assertTrue(any("не отвечает" in t for t in texts))
        self.assertIn("Снова на связи.", texts)

    def test_too_many_actions_in_a_row_forces_an_answer(self):
        model = ScriptedLLM(*[call("update_request", wishes=[f"x{i}"]) for i in range(loop.MAX_ACTIONS_IN_ROW + 5)])
        s = O.create_session("x", model)
        asyncio.run(drive(s))
        self.assertEqual(len(tool_results(s)), loop.MAX_ACTIONS_IN_ROW)
        self.assertEqual(s.messages[-1]["role"], "assistant")  # дальше — ответ клиенту, без действий

    def test_text_while_card_is_open_answers_the_card(self):
        def card_answered(messages):
            self.assertEqual(last_result(messages), {"answer": "до 9 млн"})
            return say("Записал.")
        s = O.create_session("x", ScriptedLLM(call("ask_client", question="Бюджет?", options=["до 8 млн"]), card_answered))

        async def go():
            q: asyncio.Queue = asyncio.Queue()
            s.subscribers.add(q)
            s.task = asyncio.create_task(loop.run_session(s))
            while (ev := await asyncio.wait_for(q.get(), 5))["type"] != "confirm":
                pass
            self.assertEqual(await s.receive_text("до 9 млн"), "answer")
            while not (ev := await asyncio.wait_for(q.get(), 5))["type"] == "message" or ev["role"] != "agent":
                pass
            s.task.cancel()
            return ev
        self.assertEqual(asyncio.run(go())["text"], "Записал.")

    def test_dangling_calls_closed_after_restart(self):
        memory = [{"role": "user", "content": "x"},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                        "function": {"name": "confirm_red_lines", "arguments": "{}"}}]}]
        fixed = O.close_dangling_calls(memory)
        self.assertEqual((fixed[-1]["role"], fixed[-1]["tool_call_id"]), ("tool", "c1"))
        self.assertIn("прервано", json.loads(fixed[-1]["content"])["error"])

    def test_gigachat_gets_function_call_format(self):
        msgs = to_gigachat_messages([
            {"role": "user", "content": "двушка"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                  "function": {"name": "update_request", "arguments": '{"rooms": 2}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"missing": ["city"]}'}], json_mode=False)
        self.assertEqual(msgs[1]["function_call"], {"name": "update_request", "arguments": {"rooms": 2}})
        self.assertEqual((msgs[2]["role"], msgs[2]["name"]), ("function", "update_request"))


if __name__ == "__main__":
    unittest.main()
