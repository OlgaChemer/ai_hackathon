"""Локация: расстояния, «рядом» по карте и кеш запросов к Nominatim."""
import os
import tempfile
import unittest
from pathlib import Path

from backend.sources import places


class DistanceTest(unittest.TestCase):
    def test_haversine_one_km_north(self):
        d = places.haversine(55.796, 49.106, 55.805, 49.106)
        self.assertAlmostEqual(d, 1000, delta=25)

    def test_walk_minutes(self):
        self.assertEqual(places.walk_minutes(20), 1)    # минимум минута
        self.assertEqual(places.walk_minutes(400), 5)
        self.assertEqual(places.walk_minutes(1200), 15)

    def test_map_link(self):
        link = places.map_link(55.796, 49.106)
        self.assertIn("yandex.ru/maps", link)
        self.assertIn("55.796000", link)
        self.assertIsNone(places.map_link(None, None))


class NearbyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_cache = places.CACHE
        self._orig_fetch = places._fetch_osm
        self._orig_key = places.TWOGIS_KEY
        places.CACHE = Path(self.tmp)
        places.TWOGIS_KEY = ""              # тесты не ходят в 2GIS и не ждут таймаут
        self.calls = []

        async def fake_fetch(lat, lng, kind):
            self.calls.append(kind)
            data = {
                "садик": [{"name": "Детский сад № 1", "lat": 55.8000, "lng": 49.1060}],   # ~450 м
                "школа": [{"name": "Школа № 1", "lat": 55.7975, "lng": 49.1060}],         # ~170 м
                "парк": [{"name": "Дальний парк", "lat": 55.8300, "lng": 49.1060}],       # ~3,8 км
            }
            return data.get(kind, [])

        places._fetch_osm = fake_fetch

    def tearDown(self):
        places.CACHE = self._orig_cache
        places._fetch_osm = self._orig_fetch
        places.TWOGIS_KEY = self._orig_key
        for f in os.listdir(self.tmp):
            os.unlink(Path(self.tmp) / f)

    def test_nearest_picked(self):
        res = places.nearby(55.796, 49.106, ["школа", "садик", "парк"])
        res = __import__("asyncio").run(res)
        self.assertIn("школа", res)
        self.assertIn("садик", res)
        self.assertNotIn("парк", res)                     # дальше радиуса «рядом»
        self.assertLess(res["школа"]["distance_m"], res["садик"]["distance_m"])
        self.assertEqual(res["школа"]["walk_min"], places.walk_minutes(res["школа"]["distance_m"]))

    def test_cache_avoids_second_request(self):
        import asyncio
        asyncio.run(places.nearby(55.796, 49.106, ["школа"]))
        first = len(self.calls)
        asyncio.run(places.nearby(55.796, 49.106, ["школа"]))
        self.assertEqual(len(self.calls), first)          # второй раз — из кеша

    def test_no_coords_returns_empty(self):
        import asyncio
        self.assertEqual(asyncio.run(places.nearby(None, None, ["школа"])), {})


class TwoGisFallbackTest(unittest.TestCase):
    """2GIS с зарубежного IP недоступен — молча уходим на OSM, а не ломаем подборку."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (places.CACHE, places.TWOGIS_KEY, places._fetch_2gis, places._fetch_osm, places._2gis)
        places.CACHE = Path(self.tmp)
        places.TWOGIS_KEY = "test-key"

        async def boom(lat, lng, kind):
            raise TimeoutError("2gis timeout")

        async def osm(lat, lng, kind):
            return [{"name": "Школа из OSM", "lat": lat, "lng": lng}]

        places._fetch_2gis = boom
        places._fetch_osm = osm

    def tearDown(self):
        places.CACHE, places.TWOGIS_KEY, places._fetch_2gis, places._fetch_osm, places._2gis = self._orig
        for f in os.listdir(self.tmp):
            os.unlink(Path(self.tmp) / f)

    def test_falls_back_to_osm_and_stops_trying(self):
        import asyncio
        res = asyncio.run(places.nearby(55.796, 49.106, ["школа"]))
        self.assertIn("школа", res)
        self.assertEqual(places._2gis, "down")
        self.assertIn("OSM", res["школа"]["name"])


class InfraCriterionTest(unittest.TestCase):
    """Школа рядом считается по карте, а не по словам в описании."""

    def test_school_close_and_far(self):
        from backend.agent import criteria as C
        p = {"city": "Казань", "rooms": 2, "budget_max": 9_000_000,
             "condition": "any", "metro_any": True, "infra": ["школа"]}
        base = {"city": "Казань", "rooms": 2, "price": 8_000_000}
        near = {**base, "nearby": {"школа": {"name": "Школа № 1", "distance_m": 280}}}
        far = {**base, "nearby": {"школа": {"name": "Школа № 9", "distance_m": 1600}}}
        none = {**base, "nearby": {}}
        self.assertEqual(C.check(p, near)["failed"], [])
        self.assertEqual(C.check(p, far)["failed"], ["infra"])
        self.assertEqual(C.check(p, none)["unknown"], ["infra"])


if __name__ == "__main__":
    unittest.main()
