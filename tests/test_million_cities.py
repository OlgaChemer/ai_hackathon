"""Полная проверка 18 городов на собранных геоданных (backend/geodata/cities).

Для каждого города: название во всех привычных формах и сокращениях проходит разбор модели и разбор кодом;
каждый административный район узнаётся; есть районы «у центра», «спальные», «деловые», а для «престижного»
без наших цен — неизвестность; оценки не сужают источник; «что рядом» считается локально и не выдаёт
пустую выгрузку за «рядом ничего нет»; центр города известен.

Пропускается, если геоданные собраны не полностью.
"""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import pymorphy3

from backend import db, geo, geodata, sources
from backend.agent import districts as D
from backend.agent import rule_parser as R
from backend.agent import session
from backend.agent import understanding as U
from backend.agent.actions import search as S
from backend.agent.pricing import center_distance
from backend.agent.profile import EMPTY_PROFILE
from backend.llm import LLMClient, LLMResponse, ToolCall

MORPH = pymorphy3.MorphAnalyzer()
SPECIAL_LOCT = {"Ростов-на-Дону": "Ростове-на-Дону", "Санкт-Петербург": "Санкт-Петербурге", "Нижний Новгород": "Нижнем Новгороде"}
ALIASES = {"ЕКБ": "Екатеринбург", "Екат": "Екатеринбург", "Питер": "Санкт-Петербург", "СПб": "Санкт-Петербург",
           "МСК": "Москва", "Нск": "Новосибирск", "Новосиб": "Новосибирск", "НН": "Нижний Новгород", "Челяба": "Челябинск"}


def loct(name: str) -> str:
    if name in SPECIAL_LOCT:
        return SPECIAL_LOCT[name]
    return " ".join(MORPH.parse(w)[0].inflect({"loct"}).word.capitalize() for w in name.split())


def loct_district(name: str) -> str | None:
    """«Советский» -> «советском», «Верх-Исетский» -> «верх-исетском»; несклоняемые («Силино») — None."""
    parts = name.split()
    out = []
    for part in parts:
        sub = part.split("-")
        p = MORPH.parse(sub[-1])[0]
        if "ADJF" not in p.tag:
            return None
        sub[-1] = p.inflect({"loct", "masc"}).word
        out.append("-".join(sub))
    return " ".join(out)


class ToolModel(LLMClient):
    """Как DeepSeek/GigaChat: маршрут profile_change, затем извлечение условий (profile_extract)."""
    name, model = "test", "tools"

    def __init__(self, text, changes):
        self.text, self.changes, self.seen = text, changes, []

    async def chat(self, messages, **kw):
        self.seen.append(kw["tag"])
        if kw["tag"] == "request_review":
            return LLMResponse("", [ToolCall("profile_change", {})])
        fields = [{"key": k, "value": json.dumps(v) if isinstance(v, list) else str(v), "source": self.text}
                  for k, v in self.changes.items()]
        return LLMResponse(json.dumps({"fields": fields, "criteria": [], "states": [], "filters": [], "places": [],
                                       "unsupported": [], "selection": [], "requested_card": []}, ensure_ascii=False))


class MillionCitiesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = patch.object(geodata, "OUT_DIR", geodata.DEFAULT_DIR)
        self.dir.start()
        self.addCleanup(self.dir.stop)
        geodata.reset()
        R.reset()
        self.addCleanup(geodata.reset)
        self.addCleanup(R.reset)
        if len(geodata.covered()) < len(geodata.CITIES):
            self.skipTest("геоданные собраны не полностью")
        db.configure("sqlite://")
        D._prices.clear()

    async def review(self, text, changes):
        s = session.create_session("", ToolModel(text, changes))
        event = s.push("message", role="user", text=text)
        s.accept_request(text, event["seq"])
        await U.ensure(s)
        return s

    async def test_active_rules_handle_simple_requests_for_all_eighteen_cities(self):
        with patch.object(U,'settings',replace(U.settings,rule_parser='on')):
            for city in geodata.CITIES:
                text=f"Двушка в {loct(city['name'])} до 10 млн, наличными"
                with self.subTest(city=city['name']):
                    s=await self.review(text,{'city':city['name']})
                    self.assertEqual(s.llm.seen,[])  # запросы к модели не понадобились
                    self.assertEqual(s.profile['city'],city['name'])
                    self.assertEqual(s.profile['rooms_options'],[2])
                    self.assertEqual(s.profile['budget_max'],10_000_000)
                    self.assertEqual(s.profile['payment'],'cash')

    # ------------------------------------------------------------ город

    async def test_city_in_every_form_passes_model_parsing(self):
        for c in geodata.CITIES:
            name = c["name"]
            for text in (f"Двушка в {loct(name)} до 10 млн", f"{name}, двушка до 10 млн"):
                with self.subTest(text=text):
                    self.assertEqual(sources.normalize_city(text), name)
                    s = await self.review(text, {"city": name})
                    self.assertEqual(s.profile["city"], name)
                    self.assertEqual(s.llm.seen, ["request_review", "profile_extract"])  # без повторов

    async def test_aliases_pass_model_parsing(self):
        for alias, name in ALIASES.items():
            text = f"Ищу двушку в {alias} до 15 млн"
            with self.subTest(alias=alias):
                self.assertEqual(sources.normalize_city(text), name)
                s = await self.review(text, {"city": name})
                self.assertEqual(s.profile["city"], name)

    async def test_test_drive_phrases(self):
        """Фразы из теста 08.11 — через разбор модели."""
        cases = [
            ("Ищу двухкомнатную квартиру в Уфе за 12 миллионов рублей. Для меня важен спальный район и близость к поликлинике.",
             {"city": "Уфа", "rooms_options": [2], "budget_max": 12_000_000, "district_type": "sleeping"}),
            ("хочу найти квартиру в Екатеринбурге в бизнес-районе, двухкомнатная квартира за 25 миллионов рублей.",
             {"city": "Екатеринбург", "rooms_options": [2], "budget_max": 25_000_000, "district_type": "business"}),
            ("Хочу квартиру в ЕКБ, двушку до 25 млн", {"city": "Екатеринбург", "rooms_options": [2], "budget_max": 25_000_000}),
        ]
        for text, changes in cases:
            with self.subTest(text=text):
                s = await self.review(text, changes)
                for key, value in changes.items():
                    self.assertEqual(s.profile[key], value, key)

    # ------------------------------------------------------------ районы

    def test_every_district_is_parsed_by_code(self):
        for c in geodata.CITIES:
            city = geodata.city(c["name"])
            for d in city["districts"]:
                texts = [f"двушка, район {d['name']}"]
                if loc := loct_district(d["name"]):
                    texts.append(f"двушка в {loc} районе")
                for text in texts:
                    with self.subTest(city=c["name"], text=text):
                        p = copy.deepcopy(EMPTY_PROFILE)
                        p["city"] = c["name"]
                        r = R.parse(text, p)
                        self.assertIsNotNone(r)
                        self.assertIn(d["name"], r.get("districts") or [])
                        self.assertNotIn("city", r)  # «Дзержинский район» — не город Дзержинский

    def test_code_parser_types_and_aliases(self):
        p = copy.deepcopy(EMPTY_PROFILE)
        self.assertEqual(R.parse("двушка в ЕКБ в деловом районе до 25 млн", p),
                         {"city": "Екатеринбург", "district_type": "business", "rooms_options": [2], "budget_max": 25_000_000})
        p["city"] = "Новосибирск"
        self.assertEqual(R.parse("в центральном районе", p), {"districts": ["Центральный"]})
        self.assertEqual(R.parse("в центре", p), {"prefer_center": True, "center_required": True})

    def test_every_city_has_district_types(self):
        for c in geodata.CITIES:
            with self.subTest(city=c["name"]):
                for kind in ("center", "sleeping", "business"):
                    self.assertTrue(D.for_type(c["name"], kind), kind)

    async def test_search_area_for_every_city_and_type(self):
        class Say:
            def __init__(self):self.text = []
            async def say(self, text, **kw):self.text.append(text)
        for c in geodata.CITIES:
            for kind in ("sleeping", "business", "prestige"):
                for center in (False, True):
                    with self.subTest(city=c["name"], kind=kind, center=center):
                        s = Say()
                        p = {**copy.deepcopy(EMPTY_PROFILE), "city": c["name"], "district_type": kind, "center_required": center}
                        area = await S._search_area(s, p)
                        self.assertEqual(area,())  # оценка типа не превращается в жёсткий фильтр
                        self.assertTrue(s.text and "нет данных" not in s.text[0], s.text)
            with self.subTest(city=c["name"], kind="center"):
                s = Say()
                area = await S._search_area(s, {**copy.deepcopy(EMPTY_PROFILE), "city": c["name"], "center_required": True})
                self.assertEqual(area,())
                self.assertIn('по координатам',s.text[0])

    # ------------------------------------------------------------ что рядом и центр

    def test_local_nearby_and_center(self):
        for c in geodata.CITIES:
            city = geodata.city(c["name"])
            lat, lng = city["center"]
            with self.subTest(city=c["name"]):
                self.assertLess(center_distance({"city": c["name"], "lat": lat, "lng": lng}), 1.5)  # ручная точка центра или центр OSM
                x = {"id": "t", "city": c["name"], "lat": lat, "lng": lng}
                kinds = list(geo.POI)
                geo._local([x], kinds)
                self.assertTrue(x.get('nearby'))
                for k,item in x.get('nearby',{}).items():
                    self.assertIsNotNone(item)
                    self.assertLessEqual(item['dist'],geo.RADIUS)
                    self.assertEqual(x['nearby_evidence'][k]['source'],'osm_local')
                self.assertIsNotNone(geodata.district_of(c["name"], lat, lng))


if __name__ == "__main__":
    unittest.main()
