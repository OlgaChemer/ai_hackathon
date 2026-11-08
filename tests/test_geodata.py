"""Геоданные миллионников и разбор запроса кодом — на синтетическом городе без сети.

Overpass подменён фикстурой (ответы в формате Overpass JSON): сборка scripts/build_geodata.py -> справочник
backend.geodata -> разбор agent/rule_parser -> метки районов agent/districts -> «тихо» у дома ->
«что рядом» без 2ГИС (geo._local) -> id районов Циана в ссылке -> встраивание в понимание сообщений агента.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import db, geo, geodata
from backend.agent import districts as D
from backend.agent import rule_parser as R
from backend.agent import session as O
from backend.agent import understanding as U
from backend.sources import cian
from scripts import build_geodata as B

CENTER = (55.790, 49.120)


def square(lat, lng, half=0.02):
    pts = [(lat - half, lng - half), (lat - half, lng + half), (lat + half, lng + half), (lat + half, lng - half), (lat - half, lng - half)]
    return [{"lat": a, "lon": b} for a, b in pts]


DISTRICTS = {  # id: (название в OSM, центр квадрата)
    101: ("Вахитовский район", CENTER),
    102: ("Советский район", (55.790, 49.260)),
    103: ("Приволжский район", (55.720, 49.150)),
}


def fake_overpass(query: str) -> list[dict]:
    if "rel(id:" in query:
        return [{"type": "relation", "id": rid, "members": [{"type": "way", "role": "outer", "geometry": square(*c)}]}
                for rid, (_, c) in DISTRICTS.items()]
    if 'area["boundary"="administrative"]["name"=' in query:
        return [{"type": "area", "id": 3600000001, "tags": {"name": "Казань", "admin_level": "6"}},
                {"type": "area", "id": 3600000002, "tags": {"name": "Казань", "admin_level": "2"}}]
    if '["place"="city"]' in query:
        return [{"type": "node", "id": 9, "lat": CENTER[0], "lon": CENTER[1], "tags": {"place": "city", "name": "Казань"}}]
    if 'rel(area.c)["boundary"' in query:
        return [{"type": "relation", "id": rid, "tags": {"name": name, "admin_level": "9"}, "center": {"lat": c[0], "lon": c[1]}}
                for rid, (name, c) in DISTRICTS.items()]
    if '["place"~"^(suburb' in query:
        return [{"type": "node", "id": 20, "lat": 55.795, "lon": 49.265, "tags": {"place": "suburb", "name": "Азино"}}]
    if '["station"~' in query:
        return [{"type": "node", "id": 30, "lat": 55.791, "lon": 49.121, "tags": {"station": "subway", "name": "Кремлёвская"}}]
    if '["amenity"="school"]' in query:  # школы — в Советском (спальный) и одна в центре
        pts = [(55.785 + i * 0.002, 49.255) for i in range(6)] + [(55.789, 49.118)]
        return [{"type": "node", "id": 100 + i, "lat": a, "lon": b, "tags": {"amenity": "school", "name": f"Школа №{i + 1}"}}
                for i, (a, b) in enumerate(pts)]
    if '["amenity"="kindergarten"]' in query:
        return [{"type": "node", "id": 200 + i, "lat": 55.792, "lon": 49.250 + i * 0.003, "tags": {"amenity": "kindergarten"}}
                for i in range(5)]
    if '"office"' in query or '"shop"' in query:  # деловой центр
        return [{"type": "node", "id": 300 + i, "center": {"lat": 55.788 + i * 0.0005, "lon": 49.118}} for i in range(40)] + \
               [{"type": "node", "id": 400 + i, "center": {"lat": 55.720, "lon": 49.150 + i * 0.001}} for i in range(15)]
    if '"highway"' in query:  # магистраль через центр
        return [{"type": "way", "id": 500, "geometry": [{"lat": 55.770, "lon": 49.125}, {"lat": 55.810, "lon": 49.125}]}]
    return []


class GeoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        city, extra = B.build_city({"name": "Казань"}, fetch=fake_overpass, log=lambda *_: None)
        B.write(city, extra, cls.tmp)
        cls.built = city
        cls.dir_patch = patch.object(geodata, "OUT_DIR", cls.tmp)
        cls.dir_patch.start()
        geodata.reset()
        R.reset()
        D._prices.clear()

    @classmethod
    def tearDownClass(cls):
        cls.dir_patch.stop()
        geodata.reset()
        R.reset()
        D._prices.clear()


class BuildTest(GeoTestCase):
    def test_city_built_from_osm(self):
        c = geodata.city("Казань")
        self.assertEqual(geodata.covered(), ["Казань"])
        self.assertEqual(sorted(d["name"] for d in c["districts"]), ["Вахитовский", "Приволжский", "Советский"])
        vah = next(d for d in c["districts"] if d["name"] == "Вахитовский")
        self.assertTrue(vah.get("has_center"))
        self.assertGreater(vah["area_km2"], 5)
        self.assertEqual(c["places"][0]["district"], "Советский")  # Азино привязан к району по границе
        self.assertEqual(c["metro"][0]["district"], "Вахитовский")
        self.assertEqual(len(geodata.poi_rows("Казань", "school")), 7)
        self.assertIsNone(geodata.city("Тюмень"))  # нет данных — модуль честно отвечает «нет»

    def test_lookup_and_point_in_district(self):
        self.assertEqual(geodata.lookup("азино")[0]["district"], "Советский")
        self.assertEqual(geodata.district_of("Казань", 55.79, 49.26)["name"], "Советский")
        self.assertIsNone(geodata.district_of("Казань", 56.5, 50.0))


class RuleParserTest(GeoTestCase):
    def test_districts_and_places(self):
        self.assertEqual(R.parse("двушка в Советском районе Казани до 7 млн", {}),
                         {"rooms_options": [2], "budget_max": 7_000_000, "city": "Казань", "cities": [], "districts": ["Советский"]})
        # неофициальный район -> административный; город — из профиля
        self.assertEqual(R.parse("в Азино", {"city": "Казань"}), {"districts": ["Советский"]})
        # микрорайон, который знает Циан (id из выдачи), — ищем точно по нему, а не по всему району
        with patch.object(cian, "district_ids", lambda city, names: (820,) if names == ["Азино"] else ()):
            self.assertEqual(R.parse("в Азино", {"city": "Казань"}), {"districts": ["Азино"]})
        from backend.agent.profile import in_districts
        self.assertTrue(in_districts({"district": "Советский", "cian_districts": [[820, "Азино", "mikroraion"]]}, ["Азино"]))
        # район другого города, чем в профиле, — решает модель
        self.assertIsNone(R.parse("в Советском районе", {"city": "Москва"}))
        # «советский» без «в/район» — обычное слово, не район
        self.assertIsNone(R.parse("советский дом до 7 млн", {"city": "Казань"}))
        # станция метро в профиле не хранится — модель
        self.assertIsNone(R.parse("у метро Кремлёвская", {"city": "Казань"}))

    def test_district_type_and_center(self):
        self.assertEqual(R.parse("спальный район, до 8 млн", {"city": "Казань"}), {"district_type": "sleeping", "budget_max": 8_000_000})
        self.assertEqual(R.parse("в тихом районе", {})["district_type"], "quiet")
        self.assertEqual(R.parse("в центре", {}), {"prefer_center": True, "center_required": True})
        self.assertIsNone(R.parse("спальный или престижный район", {}))

    def test_declines_to_model(self):
        for text in ["не в Москве, а в Казани", "работаю в Москве, ищу в Туле", "а в Казани дешевле?",
                     "в Москве, а нет, передумал, в Питере", "хочу в Москве рядом с парком", "ипотека",
                     "покажи первую", "позвони продавцу", "не первый этаж", "до 9"]:
            self.assertIsNone(R.parse(text, {}), text)

    def test_simple_messages(self):
        cases = {
            "Двушка в Казани до 9 млн, наличными": {"payment": "cash", "rooms_options": [2], "budget_max": 9_000_000, "city": "Казань", "cities": []},
            "однушку или двушку в Екатеринбурге, вторичка": {"housing": "secondary", "rooms_options": [1, 2], "city": "Екатеринбург", "cities": []},
            "в Москве или Подмосковье": {"city": "Москва", "cities": ["Московская область"]},
            "студия в Новосибирске за 4,5 млн": {"rooms_options": [0], "budget_max": 4_500_000, "city": "Новосибирск", "cities": []},
            "2-комнатную в Самаре до 7 млн, ипотека, взнос есть": {"payment": "mortgage_ready", "rooms_options": [2], "budget_max": 7_000_000, "city": "Самара", "cities": []},
        }
        for text, expected in cases.items():
            self.assertEqual(R.parse(text, {}), expected, text)


class DistrictLabelsTest(GeoTestCase):
    def test_labels_from_data(self):
        with patch.object(D, "price_stats", lambda city: {"city": 150_000, "districts": {
                "вахитовский": (240_000, 20), "советский": (120_000, 30), "приволжский": (140_000, 12)}}):
            labels = {k: v["labels"] for k, v in D.labels("Казань").items()}
            self.assertIn("center", labels["Вахитовский"])
            self.assertIn("business", labels["Вахитовский"])
            self.assertIn("prestige", labels["Вахитовский"])  # 240 000 / 150 000 = 1,6
            self.assertEqual(labels["Советский"], ["sleeping"])
            self.assertNotIn("sleeping", labels["Приволжский"])  # магазины — не спальный
            self.assertEqual(D.for_type("Казань", "sleeping"), ["Советский"])
            self.assertEqual(D.of_listing({"city": "Казань", "district": "Советский"}), ["sleeping"])
            self.assertEqual(D.of_listing({"city": "Казань", "lat": 55.79, "lng": 49.26}), ["sleeping"])  # район по координатам
            self.assertIsNone(D.of_listing({"city": "Тюмень", "district": "Центральный"}))

    def test_quiet_house(self):
        loud = D.quiet({"city": "Казань", "lat": 55.790, "lng": 49.126})   # ~60 м от магистрали
        calm = D.quiet({"city": "Казань", "lat": 55.790, "lng": 49.260})   # магистрали в 1 км нет
        self.assertFalse(loud["quiet"])
        self.assertLess(loud["road_m"], 100)
        self.assertTrue(calm["quiet"])
        self.assertIsNone(D.quiet({"city": "Тюмень", "lat": 57.15, "lng": 65.53}))

    def test_conditions_use_labels(self):
        from backend.agent.criteria import evaluate
        with patch.object(D, "price_stats", lambda city: {"city": None, "districts": {}}):
            sleepy = {"id": "a", "city": "Казань", "district": "Советский", "lat": 55.79, "lng": 49.26, "price": 1, "area": 40}
            central = {"id": "b", "city": "Казань", "district": "Вахитовский", "lat": 55.79, "lng": 49.126, "price": 1, "area": 40}
            want = evaluate({"district_type": "sleeping"}, sleepy)
            self.assertEqual(want["checks"][0]["status"], True)
            self.assertEqual(want["checks"][0]["source"], "osm+prices")
            self.assertEqual(evaluate({"district_type": "sleeping"}, central)["checks"][0]["status"], False)
            quiet = evaluate({"district_type": "quiet"}, central)
            self.assertIn(False, [c["status"] for c in quiet["checks"]])  # у магистрали — не тихо
            self.assertEqual(quiet["status"], "confirmed")  # «тихо» — пожелание, не отсеивает квартиру


class CityAreaFallbackTest(unittest.TestCase):
    def test_boundary_named_differently(self):
        """Граница «Пермский городской округ»: по точному имени не находится — берём область вокруг точки города."""
        def fetch(query):
            if 'area["boundary"="administrative"]["name"=' in query:
                return []
            if "is_in" in query:
                return [{"type": "area", "id": 3600000077, "tags": {"name": "Пермский городской округ", "admin_level": "6"}},
                        {"type": "area", "id": 3600000005, "tags": {"name": "Пермский край", "admin_level": "4"}},
                        {"type": "area", "id": 3600000002, "tags": {"name": "Россия", "admin_level": "2"}}]
            if '["place"="city"]' in query:
                return [{"type": "node", "id": 1, "lat": 58.01, "lon": 56.23, "tags": {"name": "Пермь"}}]
            return []
        # «Пермский край» тоже содержит основу названия, но это регион (уровень 4) — его не берём
        area_id, center = B.city_area({"name": "Пермь"}, fetch)
        self.assertEqual((area_id, center), (3600000077, (58.01, 56.23)))


class OverpassHardeningTest(unittest.TestCase):
    def test_partial_answer_is_retried(self):
        """200 с remark «runtime error» — данные неполные (так у Перми пропала ж/д): идём к следующему зеркалу."""
        class Resp:
            status_code = 200
            def __init__(self, body): self.body = body
            def json(self): return self.body
        answers = [Resp({"remark": "runtime error: Query timed out in \"query\" at line 1 after 241 seconds.", "elements": []}),
                   Resp({"elements": [{"id": 1}]})]
        with patch.object(B.httpx, "post", side_effect=lambda *a, **k: answers.pop(0)), patch.object(B, "PAUSE_S", 0):
            self.assertEqual(B.overpass("q"), [{"id": 1}])
        self.assertFalse(B.partial(""))
        self.assertTrue(B.partial("runtime error: out of memory"))

    def test_okrug_by_ring_when_center_outside(self):
        square = lambda a, b, c, d: [[a, b, a, d, c, d, c, b, a, b]]
        okrugs = [{"name": "Западный", "abbr": "ЗАО", "rings": square(0, 0, 10, 10)},
                  {"name": "Центральный", "abbr": "ЦАО", "rings": square(0, 10, 10, 20)}]
        rings = square(1, 1, 3, 3) + square(20, 20, 21, 21)  # район с эксклавом, центр посередине — вне округов
        self.assertEqual(B.okrug_of((11.0, 11.0), rings, okrugs), "ЗАО")
        self.assertEqual(B.okrug_of((5.0, 15.0), rings, okrugs), "ЦАО")
        self.assertIsNone(B.okrug_of((50.0, 50.0), square(40, 40, 41, 41), okrugs))

    def test_refresh_roads_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(B, "OUT_DIR", Path(tmp)):
            city, extra = B.build_city({"name": "Казань"}, fetch=fake_overpass, log=lambda *_: None)
            extra["roads"] = {"major": [], "rail": []}
            B.write(city, extra, Path(tmp))
            got = B.refresh_roads({"name": "Казань"}, fetch=fake_overpass, log=lambda *_: None)
            self.assertTrue(got["roads"]["major"])
            self.assertEqual(got["poi"], extra["poi"])


class RealDataTest(unittest.TestCase):
    """Проверки на собранных файлах 18 городов (пропускаются, если файлов нет)."""
    def setUp(self):
        self.dir_patch = patch.object(geodata, "OUT_DIR", geodata.DEFAULT_DIR)
        self.dir_patch.start()
        self.addCleanup(self.dir_patch.stop)
        geodata.reset()
        if len(geodata.covered()) < len(geodata.CITIES):
            self.skipTest("геоданные собраны не полностью")

    def tearDown(self):
        geodata.reset()

    def test_moscow_okrugs(self):
        c = geodata.city("Москва")
        self.assertEqual(len({d.get("okrug") for d in c["districts"]} - {None}), 12)
        self.assertTrue(all(d.get("okrug") for d in c["districts"]))

    def test_every_city_has_districts_and_center(self):
        for cfg in geodata.CITIES:
            c = geodata.city(cfg["name"])
            self.assertGreaterEqual(len(c["districts"]), 4, cfg["name"])
            self.assertTrue(any(d.get("has_center") for d in c["districts"]), cfg["name"])

    def test_lookup_real_places(self):
        self.assertIn("Санкт-Петербург", {e["city"] for e in geodata.lookup("Петроградский")})
        self.assertIn("Уфа", {e["city"] for e in geodata.lookup("Сипайлово")})
        self.assertEqual({e["city"] for e in geodata.lookup("Хамовники")}, {"Москва"})


class LocalPoiTest(GeoTestCase):
    def test_nearby_without_2gis(self):
        db.configure("sqlite://")
        x = {"id": "cian-1", "city": "Казань", "lat": 55.786, "lng": 49.256}
        far = {"id": "cian-2", "city": "Казань", "lat": 55.720, "lng": 49.150}
        with patch.object(geo, "_live", side_effect=AssertionError("Overpass не нужен")), \
                patch.object(geo.places, "enrich", side_effect=AssertionError("2ГИС не нужен")):
            ok = asyncio.run(geo.nearby([x, far], ["school"]))
        self.assertTrue(ok)
        self.assertEqual(x["nearby"]["school"]["name"], "Школа №1")
        self.assertLess(x["nearby"]["school"]["dist"], 300)
        self.assertEqual(x["nearby_evidence"]["school"]["source"], "osm_local")
        self.assertIsNone(far["nearby"]["school"])  # проверено: в 1 км школы нет


class CianDistrictIdsTest(unittest.TestCase):
    def setUp(self):
        self.file = Path(tempfile.mkdtemp()) / "districts.json"
        self.patches = [patch.object(cian, "DISTRICTS_FILE", self.file), patch.object(cian, "CACHE_DIR", self.file.parent),
                        patch.object(cian, "_district_ids", None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_learn_and_filter(self):
        listings = [{"cian_districts": [[4717, "Советский", "raion"], [9, "Казань", "location"]]},
                    {"cian_districts": [[4716, "Вахитовский", "raion"]]}]
        self.assertEqual(cian.learn_districts("Казань", listings), 3)
        self.assertTrue(self.file.exists())  # переживает перезапуск
        self.assertEqual(cian.district_ids("Казань", ["Советский", "Вахитовский"]), (4717, 4716))
        self.assertEqual(cian.district_ids("Казань", ["Советский", "Неизвестный"]), ())  # не знаем все — без фильтра
        url = cian.build_url("Казань", 2, 9_000_000, 1, None, False, (4717, 4716))
        self.assertIn("district%5B0%5D=4717", url)
        self.assertIn("district%5B1%5D=4716", url)
        self.assertNotIn("district", cian.build_url("Казань", 2, 9_000_000))

    def test_parser_keeps_ids(self):
        raw = {"cianId": 1, "totalArea": 50, "roomsCount": 2, "bargainTerms": {"priceRur": 8_000_000},
               "geo": {"districts": [{"id": 4717, "name": "Советский", "type": "raion"}], "coordinates": {"lat": 55.79, "lng": 49.26}}}
        x = cian.normalize(raw, "Казань")
        self.assertIsNotNone(x)
        self.assertEqual(x["cian_districts"], [[4717, "Советский", "raion"]])


class UnderstandingTest(GeoTestCase):
    """Разбор кодом внутри агента: on — без запросов к модели; shadow — модель, но сравнение пишется в журнал."""

    def setUp(self):
        db.configure("sqlite://")
        O.SESSIONS.clear()

    def _session(self, text, llm):
        s = O.create_session(text, llm)
        s.request_inputs = [{"seq": 1, "text": text}]
        s.request_review_seq = 0
        return s

    def test_on_skips_model(self):
        from tests.fakes import ScriptedLLM
        llm = ScriptedLLM()  # ни одного ответа: если модель вызовут — тест упадёт
        s = self._session("Двушка в Казани до 9 млн, наличными", llm)
        with patch.object(U, "settings", __import__("dataclasses").replace(U.settings, rule_parser="on")):
            review = asyncio.run(U.prepare(s))
        self.assertEqual(review["decision"], "apply")
        self.assertEqual(review["changes"]["city"], "Казань")
        self.assertEqual(review["changes"]["budget_max"], 9_000_000)
        self.assertEqual(llm.seen, [])

    def test_on_falls_back_for_hard_message(self):
        s = self._session("не в Москве, а в Казани", None)
        with patch.object(U, "settings", __import__("dataclasses").replace(U.settings, rule_parser="on")):
            self.assertIsNone(U._rule_parse(s, s.request_inputs, s.profile))

    def test_waiting_card_goes_to_model(self):
        s = self._session("в Казани", None)
        s.pending_decisions = {"clarify": {"status": "waiting"}}
        with patch.object(U, "settings", __import__("dataclasses").replace(U.settings, rule_parser="on")):
            self.assertIsNone(U._rule_parse(s, s.request_inputs, s.profile))

    def test_shadow_compare(self):
        events = []
        s = self._session("в Казани", None)
        with patch.object(U, "record", lambda event, **f: events.append((event, f))):
            U._shadow_compare(s, {"city": "Казань", "cities": []}, {"decision": "apply", "intent": "search", "changes": {"city": "Казань"}})
            U._shadow_compare(s, {"city": "Казань"}, {"decision": "apply", "intent": "search", "changes": {"city": "Москва"}})
        self.assertEqual([f["outcome"] for _, f in events], ["shadow_agree", "shadow_disagree"])


if __name__ == "__main__":
    unittest.main()
