"""Ночной обход и проверка страницы объявления."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend import db, sources
from backend.agent import negotiation as N
from backend.agent import features as FT
from backend.db import repo
from backend.db.models import Listing
from backend.sources import SourceUnavailable, cian, store
from backend.workers import refresh

PROFILE = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000, "okrug": None}


def flat(n: int, price=8_000_000) -> dict:
    return {"id": f"cian-{n}", "source": "cian", "source_name": "Циан", "city": "Казань", "rooms": 2, "price": price,
            "area": 50, "url": f"https://kazan.cian.ru/sale/flat/{n}/", "description": ""}


class RefreshTest(unittest.TestCase):
    def setUp(self):
        db.configure("sqlite://")
        store.remember([flat(1), flat(2)])
        store.track(PROFILE, new=False)

    def status(self, n):
        with db.session() as s:
            return s.get(Listing, f"cian-{n}").status

    def test_full_crawl_adds_new_and_removes_gone(self):
        async def search(*a, **k):
            return [flat(2, price=7_900_000), flat(3)], {"status": "live"}

        async def page(url):
            if "/1/" in url:
                return {"alive": False, "note": "объявление снято с публикации", "offer": None}
            return {"alive": True, "note": "объявление в продаже", "offer": None}

        with patch.object(cian, "search", search), patch.object(cian, "check_listing_page", page):
            r = asyncio.run(refresh.run_once(pages=1))
        self.assertEqual((r["status"], r["added"], r["price_changed"], r["removed"]), ("ok", 1, 1, 1))
        self.assertEqual([self.status(n) for n in (1, 2, 3)], ["removed", "active", "active"])
        self.assertEqual(store.stored(PROFILE, new=False).__len__(), 2)  # снятое в подборки не попадает
        self.assertEqual(db.safe(repo.last_crawl)["removed"], 1)

    def test_incomplete_crawl_removes_nothing(self):
        async def search(*a, **k):
            raise SourceUnavailable("капча")

        async def page(url):
            raise AssertionError("неполный обход не должен проверять пропавшие")

        with patch.object(cian, "search", search), patch.object(cian, "check_listing_page", page):
            r = asyncio.run(refresh.run_once(pages=1))
        self.assertEqual((r["status"], r["removed"]), ("failed", 0))
        self.assertEqual(self.status(1), "active")


RAW = {"cianId": 1, "bargainTerms": {"priceRur": 8_000_000}, "totalArea": "50", "kitchenArea": "18", "roomsCount": 2,
       "floorNumber": 3, "building": {"floorsCount": 9}, "geo": {"coordinates": {"lat": 55.79, "lng": 49.12}},
       "description": "Есть балкон"}


class FreshParamsAndCorrectionsTest(unittest.TestCase):
    """Параметры актуализируются по странице объявления; уточнения продавца лежат поверх и не перезаписываются."""

    def setUp(self):
        db.configure("sqlite://")
        store.remember([{**flat(1), "kitchen": 23, "area": 50}])

    def test_page_refreshes_params(self):
        async def page(url):
            return {"alive": True, "note": "объявление в продаже", "offer": RAW}

        with patch.object(cian, "check_listing_page", page):
            r = asyncio.run(sources.verify(flat(1)))
        self.assertEqual((r["listing"]["kitchen"], r["listing"]["url"]), (18.0, flat(1)["url"]))
        self.assertEqual(store.stored(PROFILE)[0]["kitchen"], 18.0)

    def test_seller_correction_survives_refresh(self):
        store.correct("cian-1", {"kitchen": 15, "balcony": True})
        store.remember([{**flat(1), "kitchen": 23, "description": "без балкона"}])  # Циан и описание — своё
        x = store.stored(PROFILE)[0]
        self.assertEqual((x["kitchen"], x["listed"]["kitchen"]), (15, 23))
        self.assertIs(FT.feature_status(x, "balcony"), True)
        with db.session() as s:
            self.assertIs(repo.load_corrections(s, ["cian-1"])["cian-1"]["balcony"], True)

    def test_corrections_are_validated(self):
        x = {"kitchen": 23, "area": 50, "floor": 3, "floors": 9, "description": "Есть балкон"}
        got = N.clean_corrections({"kitchen": "18,5", "area": 5000, "floor": 12, "year": 1975, "balcony": True,
                                   "condition": "", "unknown": 1}, x)
        self.assertEqual(got, {"kitchen": 18.5, "year": 1975})  # площадь вне диапазона, этаж выше этажности, балкон и так есть

    def test_simulated_seller_is_not_saved(self):
        from backend.agent import session as O
        from backend.agent.actions.deal import apply_corrections
        from tests.fakes import ScriptedLLM
        s = O.create_session("тест", ScriptedLLM())
        listing = {**flat(1), "kitchen": 23}
        asyncio.run(apply_corrections(s, listing, {"kitchen": 15}, persist=False))
        self.assertEqual((listing["kitchen"], listing["listed"]["kitchen"]), (15, 23))  # в этом диалоге — да
        self.assertEqual(store.stored(PROFILE)[0]["kitchen"], 23)  # в общей базе — нет
        asyncio.run(apply_corrections(s, {**flat(1), "kitchen": 23}, {"kitchen": 15}, persist=True))
        self.assertEqual(store.stored(PROFILE)[0]["kitchen"], 15)


class PageVerdictTest(unittest.TestCase):
    def test_verdicts(self):
        page = '<title>Купить 2-комн. квартиру</title> "cianId":335135197'
        self.assertEqual(cian.page_verdict(200, "", page, "335135197")[0], True)
        self.assertEqual(cian.page_verdict(200, "", page + " Объявление снято с публикации", "335135197")[0], False)
        self.assertEqual(cian.page_verdict(404, "", "", "1")[0], False)
        self.assertIsNone(cian.page_verdict(200, "", "<script>challenge('335135197')</script>", "335135197")[0])  # заглушка
        self.assertIsNone(cian.page_verdict(302, "https://www.cian.ru/captcha/", "", "1")[0])
        self.assertIsNone(asyncio.run(cian.check_listing_page("https://evil.example/sale/flat/1/"))["alive"])

    def test_seconds_until(self):
        from datetime import datetime
        self.assertEqual(refresh._seconds_until("04:00", datetime(2026, 10, 29, 3, 0)), 3600)
        self.assertEqual(refresh._seconds_until("04:00", datetime(2026, 10, 29, 5, 0)), 23 * 3600)


if __name__ == "__main__":
    unittest.main()
