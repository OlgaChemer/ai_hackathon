"""Города Циана: падежи, редирект на поддомен города, заглушка защиты от ботов."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.sources import SUPPORTED_CITIES, cian
from backend.sources.base import normalize_city

PAGE = '<title>Купить двухкомнатную квартиру в Калуге</title> "offers":[]'


class CitiesTest(unittest.TestCase):
    def test_declensions(self):
        cases = {"ищу квартиру в калуге": "Калуга", "в Ростове-на-Дону": "Ростов-на-Дону", "в туле": "Тула",
                 "в перми": "Пермь", "в сочи": "Сочи", "в нижнем новгороде": "Нижний Новгород", "спб": "Санкт-Петербург",
                 "в химках": "Химки", "в Самаре": "Самара", "в екб": "Екатеринбург"}
        for text, city in cases.items():
            self.assertEqual(normalize_city(text), city, text)

    def test_no_false_cities(self):
        for text in ["читаю объявления", "сочетание цены", "ищу 2-комнатную до 9 млн", "курсы валют"]:
            self.assertIsNone(normalize_city(text), text)

    def test_more_than_hundred_cities(self):
        self.assertGreater(len(SUPPORTED_CITIES), 100)
        self.assertIn("kazan.cian.ru", cian.build_url("Казань", 2, 6_000_000))


class CianFetchTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(cian, "CACHE_DIR", tmp), mock.patch.object(cian, "SUBS_FILE", tmp / "subs.json"),
                        mock.patch.object(cian, "_subs", None), mock.patch.object(cian, "MIN_INTERVAL", 0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fetch(self, httpx_get, browser_get=None):
        with mock.patch.object(cian, "_get_httpx", side_effect=httpx_get), \
             mock.patch.object(cian, "_get_browser_like", side_effect=browser_get or httpx_get):
            return asyncio.run(cian._fetch_page("Калуга", 2, 6_000_000, 1))

    def test_follows_redirect_to_city_subdomain_and_remembers(self):
        seen = []

        async def get(url):
            seen.append(url)
            if url.startswith("https://www.cian.ru/"):
                return 301, "https://kaluga.cian.ru/cat.php?region=4780", ""
            return 200, "", PAGE

        self.assertEqual(self.fetch(get), ([], "httpx"))
        self.assertTrue(seen[1].startswith("https://kaluga.cian.ru/cat.php?") and "maxprice=" in seen[1], seen)
        self.assertIn("kaluga.cian.ru", cian.build_url("Калуга", 2, 6_000_000))  # запомнил

    def test_bot_stub_falls_back_to_browser_like_request(self):
        async def stub(url):  # 200, но вместо выдачи — пара килобайт скрипта без заголовка
            return 200, "", "<html><script>challenge()</script></html>"

        async def browser(url):
            return 200, "", PAGE

        self.assertEqual(self.fetch(stub, browser), ([], "curl_cffi"))

    def test_captcha_and_foreign_redirects_not_followed(self):
        self.assertIsNone(cian.redirect_target("https://www.cian.ru/x", "https://www.cian.ru/captcha/?r=1"))
        self.assertIsNone(cian.redirect_target("https://www.cian.ru/x", "https://evil.example/cian.ru"))
        self.assertIsNone(cian.redirect_target("https://www.cian.ru/x", "http://kaluga.cian.ru/x"))
        self.assertEqual(cian.redirect_target("https://www.cian.ru/x", "/cat.php?a=1"), "https://www.cian.ru/cat.php?a=1")


if __name__ == "__main__":
    unittest.main()
