"""Регрессии реального 307 с cookies, резервных срезов и отказов источника."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.sources import cian
from backend.sources.base import SourceUnavailable
from tests import test_llm_and_agent as fixtures
from tests.test_llm_and_agent import listing


class CianResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        for name, value in [('CACHE_DIR', directory), ('SUBS_FILE', directory/'subdomains.json'),
                            ('_memory', {}), ('_failures', {}), ('_subs', {}), ('MIN_INTERVAL', 0)]:
            p = patch.object(cian, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_http_session_survives_same_url_307_cookie_handshake(self):
        client_type = httpx.AsyncClient
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(307, headers={'Location': str(request.url), 'Set-Cookie': 'spid=synthetic; Path=/'})
            self.assertIn('spid=synthetic', request.headers.get('cookie', ''))
            return httpx.Response(200, text=fixtures.CianParsingTest.PAGE)
        with patch.object(cian.httpx, 'AsyncClient', lambda **kw: client_type(
                **{**kw, 'transport': httpx.MockTransport(handler)})):
            status, _, page = await cian._get_httpx(cian.build_url('Казань', 2, 9_000_000))
        self.assertEqual(status, 200)
        self.assertEqual(len(cian.extract_offers(page)), 1)
        self.assertEqual(len(calls), 2)

    async def test_browser_session_is_kept_for_cookie_handshake(self):
        instances = []
        url = cian.build_url('Казань', 2, 9_000_000)
        class Browser:
            def __init__(self, **kwargs):
                self.count = 0
                instances.append(self)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, target, **kwargs):
                self.count += 1
                return SimpleNamespace(status_code=307 if self.count == 1 else 200,
                    headers={'location': url} if self.count == 1 else {}, cookies={'spid': 'synthetic'},
                    text='' if self.count == 1 else fixtures.CianParsingTest.PAGE)
        with patch('curl_cffi.requests.AsyncSession', Browser):
            response = await cian._get_browser_like(url)
        self.assertEqual(response[0], 200)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].count, 2)

    async def test_captcha_307_does_not_prevent_trying_other_profile(self):
        seen = []
        class Browser:
            def __init__(self, **kwargs):
                self.profile = kwargs['impersonate']
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, target, **kwargs):
                seen.append(target)
                if self.profile == 'safari17_0':
                    return SimpleNamespace(status_code=307, headers={'location': '/captcha/'}, text='', cookies={})
                return SimpleNamespace(status_code=200, headers={}, text=fixtures.CianParsingTest.PAGE, cookies={})
        url = cian.build_url('Казань', 2, 9_000_000)
        with patch('curl_cffi.requests.AsyncSession', Browser):
            response = await cian._get_browser_like(url)
        self.assertEqual(response[0], 200)
        self.assertEqual(seen, [url, url])  # URL капчи не открывался.
        for code in cian.REDIRECTS:
            self.assertEqual(cian._blocked(code, '/CAPTCHA/', ''), 'капча')

    async def test_self_redirect_loop_is_bounded(self):
        url = cian.build_url('Казань', 2, 9_000_000)
        get = AsyncMock(return_value=SimpleNamespace(status_code=307, headers={'location': url}, cookies={'spid':'x'}))
        response = await cian._follow_cookie_redirects(get, url)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(get.await_count, 3)

    async def test_unavailable_source_uses_snapshot_then_cooldown_for_different_budget(self):
        cian._save_snapshot('Казань', 2, [listing(city='Казань')])
        fetch = AsyncMock(side_effect=SourceUnavailable('капча'))
        with patch.object(cian, '_fetch_page', fetch):
            for budget in [9_000_000, 10_000_000]:
                items, report = await cian.search('Казань', 2, budget)
                self.assertEqual(len(items), 1)
                self.assertEqual(report['status'], 'snapshot')
        self.assertEqual(fetch.await_count, 1)

    async def test_cooldown_does_not_affect_another_city_or_market(self):
        cian._failures[('Казань', False)] = (float('inf'), 'капча')
        fetch = AsyncMock(return_value=([], 'httpx'))
        with patch.object(cian, '_fetch_page', fetch):
            await cian.search('Москва', 2, 20_000_000)
            await cian.search('Казань', 2, 9_000_000, new=True)
        self.assertEqual(fetch.await_count, 2)

    async def test_timeout_releases_request_and_uses_snapshot(self):
        cancelled = asyncio.Event()
        async def stalled(*args):
            try: await asyncio.Future()
            finally: cancelled.set()
        cian._save_snapshot('Казань', 2, [listing(city='Казань')])
        with patch.object(cian, '_fetch_page', stalled), patch.object(cian, 'FETCH_TIMEOUT', .01):
            items, report = await cian.search('Казань', 2, 9_000_000)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(report['status'], 'snapshot')
        self.assertEqual(len(items), 1)

    async def test_corrupt_snapshot_never_turns_source_error_into_server_error(self):
        path = cian._snapshot_path('Казань', 2)
        for data in ['{partial', '[]', '{"version":2,"listings":null}', '{"version":2,"listings":[null,{}]}']:
            path.write_text(data)
            self.assertEqual(cian.load_snapshot('Казань', 2), ([], None))
        with patch.object(cian, '_fetch_page', AsyncMock(side_effect=SourceUnavailable('капча'))):
            with self.assertRaises(SourceUnavailable):
                await cian.search('Казань', 2, 9_000_000)

    async def test_snapshot_write_failure_keeps_previous_complete_file(self):
        cian._save_snapshot('Казань', 2, [listing(id='old')])
        with patch.object(cian.json, 'dump', side_effect=OSError('disk full')):
            cian._save_snapshot('Казань', 2, [listing(id='new')])
        self.assertEqual([x['id'] for x in cian.load_snapshot('Казань', 2)[0]], ['old'])
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    async def test_json_whitespace_is_accepted(self):
        self.assertEqual(len(cian.extract_offers(fixtures.CianParsingTest.PAGE.replace('"offers":[', '"offers" : [\n'))), 1)
