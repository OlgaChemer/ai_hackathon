"""Контракт 2GIS, бюджет времени, источник данных и сохранение частичных результатов."""
import asyncio
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx

from backend import db, geo
from backend.config import settings
from backend.db import repo
from backend.sources import places, store
from tests.test_store import flat


class PlacesTest(unittest.TestCase):
    def setUp(self):
        db.configure('sqlite://')
        geo._live_cache.clear()
        places._retry_at = 0
        places._last_provider = 'auto'
        self.config = replace(settings, twogis_api_key='synthetic-key', places_budget_s=.3)
        self.client_type = httpx.AsyncClient
        self.ids = 0

    def row(self, **kwargs):
        self.ids += 1
        x = flat(id_=f'cian-{self.ids}', lat=55.796, lng=49.106, **kwargs)
        store.remember([x])
        return x

    def transport(self, handler):
        return patch.object(places.httpx, 'AsyncClient', lambda **kwargs: self.client_type(transport=httpx.MockTransport(handler), **kwargs))

    def test_success_is_cached_with_source_timestamp_and_coordinates(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.params['rubric_id'], '245')
            self.assertEqual(request.url.params['point'], '49.106,55.796')
            return httpx.Response(200, json={'meta': {'code': 200}, 'result': {'total': 1, 'items': [
                {'name': 'Школа № 1', 'point': {'lat': 55.797, 'lon': 49.106}}]}})
        x = self.row()
        async def go():
            with self.transport(handler), patch.object(geo, 'settings', self.config), patch.object(places, 'settings', self.config), \
                 patch.object(geo, '_live', AsyncMock(side_effect=AssertionError('OSM not required'))):
                self.assertTrue(await geo.nearby([x], ['school']))
                again = {k: v for k, v in x.items() if k not in ('nearby', 'nearby_evidence')}
                self.assertTrue(await geo.nearby([again], ['school']))
                self.assertEqual(again['nearby_evidence']['school']['source'], '2gis')
        asyncio.run(go())
        self.assertEqual(len(calls), 1)
        record = db.safe(repo.load_features, [x['id']])[x['id']]['poi:school']
        self.assertEqual(record['coords'], [55.796, 49.106])
        self.assertTrue(record['evidence']['checked_at'])
        self.assertEqual(geo.summary(x, ['school'])[0]['source'], '2gis')

    def test_empty_success_is_absent_not_an_error(self):
        def handler(request):
            return httpx.Response(200, json={'meta': {'code': 200}, 'result': {'total': 0, 'items': []}})
        x = self.row()
        async def go():
            with self.transport(handler), patch.object(geo, 'settings', self.config), patch.object(places, 'settings', self.config):
                self.assertTrue(await geo.nearby([x], ['school']))
        asyncio.run(go())
        self.assertIn('school', x['nearby'])
        self.assertIsNone(x['nearby']['school'])
        self.assertEqual(x['nearby_evidence']['school']['source'], '2gis')

    def test_http_200_error_uses_osm_and_recovers_after_cooldown(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            return httpx.Response(200, json={'meta': {'code': 403}})
        async def osm(rows, kinds):
            for x in rows:
                x.setdefault('nearby', {})['school'] = {'name': 'Школа OSM', 'dist': 100}
                x.setdefault('nearby_evidence', {})['school'] = {'source': 'osm'}
            return True
        async def go():
            with self.transport(handler), patch.object(geo, 'settings', self.config), patch.object(places, 'settings', self.config), patch.object(geo, '_live', osm):
                self.assertTrue(await geo.nearby([self.row()], ['school']))
                self.assertTrue(await geo.nearby([self.row()], ['school']))
                self.assertEqual(len(attempts), 1)
                places._retry_at = 0
                self.assertTrue(await geo.nearby([self.row()], ['school']))
        asyncio.run(go())
        self.assertEqual(len(attempts), 2)

    def test_malformed_points_never_cached_as_absence(self):
        def handler(request):
            return httpx.Response(200, json={'meta': {'code': 200}, 'result': {'total': 1, 'items': [{'name': 'Без координат'}]}})
        x = self.row()
        async def go():
            with self.transport(handler), patch.object(geo, 'settings', self.config), patch.object(places, 'settings', self.config), \
                 patch.object(geo, '_live', AsyncMock(return_value=False)):
                self.assertFalse(await geo.nearby([x], ['school']))
        asyncio.run(go())
        self.assertNotIn('school', x.get('nearby', {}))
        self.assertNotIn('poi:school', db.safe(repo.load_features, [x['id']]).get(x['id'], {}))

    def test_deadline_preserves_completed_results(self):
        async def enrich(rows, kinds, radius):
            rows[0].setdefault('nearby', {})['school'] = {'name': 'Школа', 'dist': 100}
            rows[0].setdefault('nearby_evidence', {})['school'] = {'source': '2gis'}
            await asyncio.sleep(10)
        async def osm(rows, kinds):
            await asyncio.sleep(10)
        x = self.row()
        async def go():
            with patch.object(geo, 'settings', self.config), patch.object(places, 'enrich', enrich), patch.object(geo, '_live', osm):
                start = asyncio.get_running_loop().time()
                self.assertFalse(await geo.nearby([x], ['school', 'park'], budget_s=.03))
                self.assertLess(asyncio.get_running_loop().time() - start, .3)
        asyncio.run(go())
        self.assertIn('school', x['nearby'])
        self.assertNotIn('park', x['nearby'])
        cached = db.safe(repo.load_features, [x['id']])[x['id']]
        self.assertIn('poi:school', cached)
        self.assertNotIn('poi:park', cached)

    def test_no_coordinates_are_unknown(self):
        self.assertFalse(asyncio.run(geo.nearby([{'id': 'missing'}], ['school'])))

    def test_changed_coordinates_invalidate_cached_poi(self):
        x = self.row()
        record = {'version': 2, 'coords': [0, 0], 'value': {'name': 'Другой город', 'dist': 10}, 'evidence': {'source': '2gis'}}
        db.safe(repo.save_features, [(x['id'], 'poi:school', record, 'map')])
        self.assertFalse(asyncio.run(geo.nearby([x], ['school'], budget_s=0)))
        self.assertNotIn('school', x.get('nearby', {}))

    def test_no_key_still_uses_osm(self):
        async def osm(rows, kinds):
            for x in rows:
                x['nearby'] = {'school': None}
            return True
        async def go():
            with patch.object(geo, 'settings', replace(self.config, twogis_api_key='')), patch.object(geo, '_live', osm):
                self.assertTrue(await geo.nearby([self.row()], ['school']))
        asyncio.run(go())


class CianFingerprintTest(unittest.TestCase):
    def test_safari_challenge_falls_back_to_chrome_without_automatic_redirects(self):
        from backend.sources import cian
        calls = []
        class FakeSession:
            def __init__(self, **kwargs): self.profile = kwargs['impersonate']
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, url, **kwargs):
                calls.append((self.profile, kwargs['allow_redirects']))
                class Response:
                    status_code = 200
                    headers = {}
                    text = 'challenge' if self.profile == 'safari17_0' else '<title>Квартиры в Казани</title>'
                return Response()
        with patch('curl_cffi.requests.AsyncSession', FakeSession):
            result = asyncio.run(cian._get_browser_like('https://kazan.cian.ru/cat.php'))
        self.assertIn('<title>', result[2])
        self.assertEqual(calls, [('safari17_0', False), ('chrome', False)])
