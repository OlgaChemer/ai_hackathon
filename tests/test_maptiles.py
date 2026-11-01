"""Карта 2ГИС: изображения через backend, без утечки ключа и повторов при отказе API."""
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from backend import db, maptiles, ratelimit
from backend.config import settings
from backend.main import app

PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic tile'


class MapTilesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db.configure('sqlite://')
        ratelimit.reset()
        maptiles._cache.clear()
        maptiles._retry_at = 0
        self.config = patch.object(maptiles, 'settings', replace(settings, twogis_api_key='private-test-key'))
        self.config.start()
        self.client_type = httpx.AsyncClient
        self.browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.browser.aclose()
        self.config.stop()
        maptiles._cache.clear()
        maptiles._retry_at = 0
        ratelimit.reset()

    def upstream(self, handler):
        return patch.object(maptiles.httpx, 'AsyncClient', lambda **kw: self.client_type(transport=httpx.MockTransport(handler), **kw))

    async def test_server_key_stays_private_and_tile_is_cached(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.host, 'tile0.maps.2gis.com')
            self.assertEqual(request.url.params['key'], 'private-test-key')
            return httpx.Response(200, content=PNG, headers={'Content-Type': 'image/png'})
        config = await self.browser.get('/api/map')
        self.assertEqual(config.json(), {'provider': '2gis', 'tiles': '/api/map/tiles/{z}/{x}/{y}.png'})
        with self.upstream(handler):
            for _ in range(2):
                response = await self.browser.get('/api/map/tiles/15/20853/10240.png')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, PNG)
                self.assertEqual(response.headers['content-type'], 'image/png')
                self.assertIn('max-age=', response.headers['cache-control'])
                self.assertNotIn('private-test-key', str(response.headers))
        self.assertEqual(len(calls), 1)

    async def test_invalid_coordinates_never_reach_upstream(self):
        def unexpected(request):
            self.fail('invalid coordinates reached 2GIS')
        with self.upstream(unexpected):
            for path in ('20/1/1', '2/4/0', '2/0/-1'):
                response = await self.browser.get(f'/api/map/tiles/{path}.png')
                self.assertEqual(response.status_code, 404)

    async def test_denied_access_uses_cooldown_without_leaking_response_or_key(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403, text='private-test-key: subscription denied')
        with self.upstream(handler):
            for _ in range(2):
                response = await self.browser.get('/api/map/tiles/15/20853/10240.png')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertNotIn('private-test-key', response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual((await self.browser.get('/api/map')).json()['provider'], 'osm')

    async def test_network_failure_is_reported_without_upstream_url(self):
        def handler(request):
            raise httpx.ConnectError(str(request.url), request=request)
        with self.upstream(handler):
            response = await self.browser.get('/api/map/tiles/15/20853/10240.png')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-test-key', response.text)

    async def test_html_error_is_not_served_as_a_tile(self):
        with self.upstream(lambda request: httpx.Response(200, text='<html>error</html>')):
            response = await self.browser.get('/api/map/tiles/15/20853/10240.png')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('html', response.text)

    async def test_no_key_keeps_osm_working(self):
        with patch.object(maptiles, 'settings', replace(settings, twogis_api_key='')):
            self.assertEqual((await self.browser.get('/api/map')).json(), {'provider': 'osm'})
            response = await self.browser.get('/api/map/tiles/15/20853/10240.png')
            self.assertEqual(response.status_code, 503)
