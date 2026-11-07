"""Разговор меняет стиль ответа, но не параметры квартиры и не согласия."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import db, main, ratelimit
from backend.agent import loop, prompts, qa, session
from tests.fakes import ScriptedLLM, say


class VoicePromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_voice_style_is_applied_to_agent_and_faq_only_when_enabled(self):
        db.configure("sqlite://")
        llm = ScriptedLLM(say("Ответ."), say("Ответ."))
        s = session.create_session("", llm)
        s.voice_mode = True
        await loop.think(s, True)
        self.assertIn(prompts.VOICE_STYLE, llm.seen[-1][0]["content"])
        await qa.answer(s, "Что такое эскроу?")
        self.assertIn(prompts.VOICE_STYLE, llm.seen[-1][0]["content"])
        s.voice_mode = False
        await loop.think(s, True)
        self.assertNotIn(prompts.VOICE_STYLE, llm.seen[-1][0]["content"])
        self.assertFalse(s.red_lines_ok)
        self.assertEqual(s.profile, {})


class VoiceTransportTest(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")
        ratelimit.reset()
        session.SESSIONS.clear()
        self.model = patch.object(main, "get_llm", lambda: ScriptedLLM())
        self.model.start()
        self.client = TestClient(main.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.model.stop()
        session.SESSIONS.clear()

    def test_start_continue_and_return_to_text(self):
        response = self.client.post(
            "/api/session",
            json={"query": "Казань, двушка до 42 млн", "voice_mode": True},
        )
        self.assertEqual(response.status_code, 200)
        sid = response.json()["id"]
        s = session.SESSIONS[sid]
        self.assertTrue(s.voice_mode)
        response = self.client.post(
            f"/api/chat/{sid}/message",
            json={"text": "Пока не ищи", "voice_mode": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(s.voice_mode)
        s.remember()
        session.SESSIONS.pop(sid)
        response = self.client.post(
            f"/api/chat/{sid}/message",
            json={"text": "Продолжим разговор", "voice_mode": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(session.SESSIONS[sid].voice_mode)
        self.assertFalse(session.SESSIONS[sid].red_lines_ok)

    def test_mode_does_not_approve_a_card_and_invalid_flag_is_rejected(self):
        response = self.client.post(
            "/api/session", json={"query": "Казань, двушка", "voice_mode": "yes"}
        )
        self.assertEqual(response.status_code, 422)
        sid = self.client.post("/api/session", json={"query": "Казань, двушка"}).json()[
            "id"
        ]
        s = session.SESSIONS[sid]
        response = self.client.post(
            f"/api/session/{sid}/confirm",
            json={"kind": "red_lines", "approved": True, "voice_mode": True},
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(s.red_lines_ok)
