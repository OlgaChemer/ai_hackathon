"""Аккаунты и история диалогов: вход, доступ только к своим диалогам, повтор ленты, продолжение диалога.

Без сети: модель агента — поддельная, по сценарию (записать запрос, показать рамки), база — SQLite в памяти.
Агент останавливается на «красных линиях» — до поиска на Циане дело не доходит.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth, db, ratelimit
from backend import main as M
from backend.agent import session as O
from tests.fakes import ScriptedLLM, call, last_result, say
from tests.streaming import BoundedStream

QUERY = "Ищу 2-комнатную в Казани до 9 млн"


def start_model() -> ScriptedLLM:
    """Агент записывает запрос и показывает рамки — дальше ждёт клиента."""
    return ScriptedLLM(call("update_request", city="Казань", rooms=2, budget_max=9_000_000), call("confirm_red_lines"))


def read_until(client: TestClient, url: str, stop) -> list[dict]:
    """Читает SSE-ленту, пока stop(событие) не вернёт True."""
    out = []
    client.app.stop = stop
    with client.stream("GET", url) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] in ("typing", "model_wait"):  # временные сигналы — не события ленты
                continue
            out.append(ev)
            if stop(ev):
                break
    return out


class ChatsAndAuthTest(unittest.TestCase):
    def setUp(self):
        ratelimit.reset()
        db.configure("sqlite://")
        auth._fails.clear()
        O.SESSIONS.clear()
        self.models: list[ScriptedLLM] = []  # что подставить при следующем создании или подъёме агента
        self.llm = patch.object(M, "get_llm", lambda: self.models.pop(0) if self.models else start_model())
        self.llm.start()
        self.startup = patch.object(M.app.router, "on_startup", [])  # без Whisper и уборщика
        self.startup.start()

    def tearDown(self):
        for s in O.SESSIONS.values():
            if s.task and not s.task.done():
                s.task.cancel()
        self.llm.stop()
        self.startup.stop()

    def client(self) -> TestClient:
        return TestClient(BoundedStream(M.app), base_url="https://testserver")

    def start_chat(self, c: TestClient) -> tuple[str, list[dict]]:
        sid = c.post("/api/session", json={"query": QUERY}).json()["id"]
        evs = read_until(c, f"/api/chat/{sid}/stream", lambda e: e["type"] == "confirm" and e["kind"] == "red_lines")
        return sid, evs

    def test_register_login_logout(self):
        with self.client() as c:
            self.assertIsNone(c.get("/api/me").json()["user"])
            r = c.post("/api/auth/register", json={"email": "Anna@Example.ru", "password": "секрет123", "name": "Анна"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.get("/api/me").json()["user"]["email"], "anna@example.ru")
            self.assertEqual(c.post("/api/auth/register", json={"email": "anna@example.ru", "password": "другой12"}).status_code, 409)
            c.post("/api/auth/logout")
            self.assertIsNone(c.get("/api/me").json()["user"])
            self.assertEqual(c.post("/api/auth/login", json={"email": "anna@example.ru", "password": "не тот"}).status_code, 401)
            self.assertEqual(c.post("/api/auth/login", json={"email": "anna@example.ru", "password": "секрет123"}).status_code, 200)
            self.assertEqual(c.get("/api/me").json()["user"]["name"], "Анна")

    def test_password_guessing_is_limited(self):
        with self.client() as c:
            c.post("/api/auth/register", json={"email": "a@b.ru", "password": "секрет123"})
            codes = [c.post("/api/auth/login", json={"email": "a@b.ru", "password": f"x{i}"}).status_code for i in range(6)]
            self.assertEqual(codes, [401] * 5 + [429])

    def test_chat_is_private_replays_and_continues(self):
        self.models.append(ScriptedLLM(
            call("update_request", city="Казань", rooms=2, budget_max=9_000_000), call("confirm_red_lines"),
            say("Что поменять в запросе?"),
            call("update_request", rooms=3), call("confirm_red_lines")))
        with self.client() as a, self.client() as b:
            sid, evs = self.start_chat(a)
            seqs = [e["seq"] for e in evs]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(evs[0]["type"], "replay_end")  # новый диалог: истории нет, дальше — живые события
            # чужой браузер не видит диалог и не может им управлять
            self.assertEqual(b.get(f"/api/chat/{sid}/stream").status_code, 404)
            self.assertEqual(b.post(f"/api/session/{sid}/confirm", json={"kind": "red_lines"}).status_code, 404)
            self.assertEqual(b.get("/api/chats").json()["chats"], [])
            # открыли снова: сначала вся история, потом replay_end с live=True — агент ждёт решения
            again = read_until(a, f"/api/chat/{sid}/stream", lambda e: e["type"] == "replay_end")
            self.assertTrue(all(e.get("replay") for e in again[:-1]))
            self.assertEqual([e["seq"] for e in again[:-1]], [e["seq"] for e in evs[1:]])
            self.assertTrue(again[-1]["live"])
            # решение клиента — в ленте с подписью кнопки; агент спрашивает, что поменять
            r = a.post(f"/api/session/{sid}/confirm", json={"kind": "red_lines", "approved": False, "data": {"_label": "Изменить запрос"}})
            self.assertEqual(r.status_code, 200)
            tail = read_until(a, f"/api/chat/{sid}/stream?after={evs[-1]['seq']}",
                              lambda e: e["type"] == "message" and e["role"] == "agent")
            resolved = next(e for e in tail if e["type"] == "resolved")
            self.assertEqual((resolved["kind"], resolved["label"]), ("red_lines", "Изменить запрос"))
            # агент работает и ждёт: сообщение — ему, он сам меняет запрос и снова показывает рамки
            r = a.post(f"/api/chat/{sid}/message", json={"text": "а если трёшку?"}).json()
            self.assertEqual((r["continued"], r["handled_as"]), (False, "message"))
            more = read_until(a, f"/api/chat/{sid}/stream?after={tail[-1]['seq']}",
                              lambda e: e["type"] == "confirm" and e["kind"] == "red_lines")
            red = more[-1]["payload"]
            self.assertEqual((red["city"], red["rooms"], red["budget_max"]), ("Казань", 3, 9_000_000))
            self.assertGreater(more[-1]["seq"], tail[-1]["seq"])  # нумерация продолжается

    def test_agent_restarts_with_memory(self):
        """Сервер перезапустился, пока агент ждал решения: сообщение поднимает агента с памятью диалога."""
        with self.client() as a:
            sid, original = self.start_chat(a)
            for s in list(O.SESSIONS.values()):  # «перезапуск»: задачи и память процесса пропали
                s.task.cancel()
            O.SESSIONS.clear()
            seen = []

            def answer(messages):
                seen.extend(messages)
                return say("Безопасные расчёты — деньги уходят продавцу только после регистрации права.")
            class FAQModel(ScriptedLLM):
                async def chat(self,messages,**kw):
                    if kw.get('tag')=='answer':return answer(messages)
                    return await super().chat(messages,**kw)
            self.models.append(FAQModel())
            r = a.post(f"/api/chat/{sid}/message", json={"text": "Что такое безопасные расчёты?"}).json()
            self.assertTrue(r["continued"])
            tail = read_until(a, f"/api/chat/{sid}/stream?after={r['after']}",
                              lambda e: e["type"] == "message" and e["role"] == "agent")
            self.assertIn("после регистрации", tail[-1]["text"])
            self.assertFalse(any(e["type"] == "confirm" for e in tail))  # ни рамок заново, ни поиска
            self.assertEqual(seen[2]['content'],'Что такое безопасные расчёты?')
            restored=O.SESSIONS[sid]
            self.assertEqual(restored.profile['city'],'Казань')
            self.assertEqual(restored.profile['budget_max'],9_000_000)
            self.assertEqual(restored.pending_decisions['red_lines']['decision_id'],original[-1]['decision_id'])

    def test_login_moves_browser_chats_to_account(self):
        with self.client() as phone, self.client() as laptop:
            sid, _ = self.start_chat(phone)
            phone.post("/api/auth/register", json={"email": "ivan@ya.ru", "password": "секрет123"})
            self.assertEqual(laptop.get(f"/api/chat/{sid}/stream").status_code, 404)
            laptop.post("/api/auth/login", json={"email": "ivan@ya.ru", "password": "секрет123"})
            self.assertEqual([c["id"] for c in laptop.get("/api/chats").json()["chats"]], [sid])
            evs = read_until(laptop, f"/api/chat/{sid}/stream", lambda e: e["type"] == "replay_end")
            self.assertTrue(any(e["type"] == "confirm" for e in evs))

    def test_logout_hides_account_chats_in_this_browser(self):
        """Подбор, перешедший в аккаунт, после выхода браузер больше не видит — только вошедший владелец."""
        with self.client() as c:
            sid, _ = self.start_chat(c)
            r = c.post("/api/auth/register", json={"email": "anna@ya.ru", "password": "секрет123"}).json()
            self.assertEqual(r["moved_chats"], 1)
            c.post("/api/auth/logout")
            self.assertEqual(c.get("/api/chats").json()["chats"], [])
            self.assertEqual(c.get(f"/api/chat/{sid}/stream").status_code, 404)
            self.assertEqual(c.post(f"/api/chat/{sid}/message", json={"text": "ещё"}).status_code, 404)
            # новый анонимный подбор в том же браузере виден, а при входе переходит в аккаунт
            sid2, _ = self.start_chat(c)
            self.assertEqual([x["id"] for x in c.get("/api/chats").json()["chats"]], [sid2])
            r = c.post("/api/auth/login", json={"email": "anna@ya.ru", "password": "секрет123"}).json()
            self.assertEqual(r["moved_chats"], 1)
            self.assertEqual({x["id"] for x in c.get("/api/chats").json()["chats"]}, {sid, sid2})

    def test_other_account_on_same_browser_sees_only_its_chats(self):
        """Общий компьютер: второй человек входит в свой аккаунт в том же браузере — чужие подборы не видны и не переходят."""
        with self.client() as c:
            sid_a, _ = self.start_chat(c)
            c.post("/api/auth/register", json={"email": "a@ya.ru", "password": "секрет123"})
            c.post("/api/auth/logout")
            r = c.post("/api/auth/register", json={"email": "b@ya.ru", "password": "секрет123"}).json()
            self.assertEqual(r["moved_chats"], 0)
            self.assertEqual(c.get("/api/chats").json()["chats"], [])
            self.assertEqual(c.get(f"/api/chat/{sid_a}/stream").status_code, 404)
            c.post("/api/auth/logout")
            c.post("/api/auth/login", json={"email": "a@ya.ru", "password": "секрет123"})
            self.assertEqual([x["id"] for x in c.get("/api/chats").json()["chats"]], [sid_a])

    def test_live_dialogue_moves_on_login_and_continues(self):
        """Подбор идёт прямо сейчас: после входа он в аккаунте, с другого устройства открывается и продолжается."""
        with self.client() as phone, self.client() as laptop:
            sid, _ = self.start_chat(phone)
            self.assertIsNone(O.SESSIONS[sid].user_id)
            phone.post("/api/auth/register", json={"email": "live@ya.ru", "password": "секрет123"})
            self.assertIsNotNone(O.SESSIONS[sid].user_id)
            laptop.post("/api/auth/login", json={"email": "live@ya.ru", "password": "секрет123"})
            evs = read_until(laptop, f"/api/chat/{sid}/stream", lambda e: e["type"] == "replay_end")
            self.assertTrue(any(e["type"] == "confirm" for e in evs))

    def test_all_anonymous_chats_move_in_any_state(self):
        """Несколько анонимных подборов в разных состояниях — при регистрации переходят ВСЕ и открываются с другого устройства.
        live — агент ждёт ответа в памяти; evicted — выгружен из памяти (только в базе); fresh — создан, ленту не открывали."""
        with self.client() as phone, self.client() as laptop:
            live, _ = self.start_chat(phone)
            evicted, _ = self.start_chat(phone)
            O.SESSIONS[evicted].task.cancel()
            O.SESSIONS.pop(evicted)  # как после уборки простаивающих или перезапуска
            fresh = phone.post("/api/session", json={"query": "Студия в Москве до 12 млн"}).json()["id"]
            r = phone.post("/api/auth/register", json={"email": "many@ya.ru", "password": "секрет123"}).json()
            self.assertEqual(r["moved_chats"], 3)
            self.assertEqual({c["id"] for c in phone.get("/api/chats").json()["chats"]}, {live, evicted, fresh})
            laptop.post("/api/auth/login", json={"email": "many@ya.ru", "password": "секрет123"})
            self.assertEqual({c["id"] for c in laptop.get("/api/chats").json()["chats"]}, {live, evicted, fresh})
            for sid in (live, evicted):
                evs = read_until(laptop, f"/api/chat/{sid}/stream", lambda e: e["type"] == "replay_end")
                self.assertTrue(any(e["type"] == "confirm" for e in evs), sid)
            # живой подбор продолжается с другого устройства, выгруженный поднимается из базы с владельцем
            self.assertEqual(laptop.post(f"/api/chat/{live}/message", json={"text": "наличными"}).status_code, 200)
            self.assertEqual(laptop.post(f"/api/chat/{evicted}/message", json={"text": "наличными"}).status_code, 200)
            self.assertIsNotNone(O.SESSIONS[evicted].user_id)

    def test_chats_survive_server_restart_after_login(self):
        """Перенос записан в базу: после перезапуска сервера (память пуста) подборы по-прежнему в аккаунте."""
        with self.client() as phone:
            a, _ = self.start_chat(phone)
            b, _ = self.start_chat(phone)
            phone.post("/api/auth/register", json={"email": "restart@ya.ru", "password": "секрет123"})
        for s in O.SESSIONS.values():
            if s.task and not s.task.done():
                s.task.cancel()
        O.SESSIONS.clear()
        with self.client() as laptop:
            laptop.post("/api/auth/login", json={"email": "restart@ya.ru", "password": "секрет123"})
            self.assertEqual({c["id"] for c in laptop.get("/api/chats").json()["chats"]}, {a, b})
            evs = read_until(laptop, f"/api/chat/{a}/stream", lambda e: e["type"] == "replay_end")
            self.assertTrue(any(e["type"] == "confirm" for e in evs))

    def test_login_on_other_browser_does_not_take_this_browsers_chats(self):
        """Перенос — из того браузера, где выполнен вход. Подборы телефона не переходят, если войти только на ноутбуке."""
        with self.client() as phone, self.client() as laptop:
            laptop.post("/api/auth/register", json={"email": "two@ya.ru", "password": "секрет123"})
            sid, _ = self.start_chat(phone)
            self.assertEqual(laptop.get("/api/chats").json()["chats"], [])
            r = phone.post("/api/auth/login", json={"email": "two@ya.ru", "password": "секрет123"}).json()
            self.assertEqual(r["moved_chats"], 1)
            self.assertEqual([c["id"] for c in laptop.get("/api/chats").json()["chats"]], [sid])

    def test_chat_started_during_login_is_not_lost(self):
        """Гонка: подбор создан анонимно уже после переноса (вкладка не знала о входе). При следующем открытии
        списка в этом браузере под аккаунтом он тоже переходит в аккаунт."""
        with self.client() as phone, self.client() as laptop:
            phone.post("/api/auth/register", json={"email": "race@ya.ru", "password": "секрет123"})
            vid = phone.cookies.get("vid")
            late = O.create_session(QUERY, start_model(), visitor_id=vid, user_id=None)  # создан как анонимный
            self.assertEqual([c["id"] for c in phone.get("/api/chats").json()["chats"]], [late.id])
            laptop.post("/api/auth/login", json={"email": "race@ya.ru", "password": "секрет123"})
            self.assertEqual([c["id"] for c in laptop.get("/api/chats").json()["chats"]], [late.id])

    def test_two_tabs_live_dialogue_after_login_in_other_tab(self):
        """Две вкладки одного браузера: в одной идёт подбор, в другой выполнен вход — первая продолжает работать."""
        with self.client() as tab:
            sid, _ = self.start_chat(tab)
            tab.post("/api/auth/register", json={"email": "tabs@ya.ru", "password": "секрет123"})  # cookie общие
            self.assertEqual(tab.post(f"/api/chat/{sid}/message", json={"text": "наличными"}).status_code, 200)
            evs = read_until(tab, f"/api/chat/{sid}/stream", lambda e: e["type"] == "replay_end")
            self.assertTrue(evs)

    def test_idle_sessions_are_released(self):
        s = O.create_session(QUERY, ScriptedLLM())
        self.assertEqual(O.reap_idle(now=s.idle_since + O.IDLE_DONE_S + 1), [s.id])  # не запускался и никто не смотрит
        self.assertNotIn(s.id, O.SESSIONS)


if __name__ == "__main__":
    unittest.main()
