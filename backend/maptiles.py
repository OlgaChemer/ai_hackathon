"""2GIS Raster Tiles: фиксированный upstream, ключ только на сервере, ограниченный кэш.

https://docs.2gis.com/maps/others/rastertiles/overview
"""
from __future__ import annotations

import time
from collections import OrderedDict

import httpx

from .config import settings

MAX_TILES = 256
TTL = 300
_cache: OrderedDict[tuple[int, int, int], tuple[float, bytes]] = OrderedDict()
_retry_at = 0.0


class TileUnavailable(Exception):
    pass


def configuration() -> dict:
    # Ответ не содержит ни каталожного ключа, ни адреса upstream с ключом.
    if settings.twogis_api_key and time.monotonic() >= _retry_at:
        return {'provider': '2gis', 'tiles': '/api/map/tiles/{z}/{x}/{y}.png'}
    return {'provider': 'osm'}


async def tile(z: int, x: int, y: int) -> bytes:
    global _retry_at
    if not 0 <= z <= 19 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
        raise ValueError('invalid tile coordinates')
    key = (z, x, y)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < TTL:
        _cache.move_to_end(key)
        return cached[1]
    if not settings.twogis_api_key or now < _retry_at:
        raise TileUnavailable()
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f'https://tile0.maps.2gis.com/v2/tiles/online_sd/{z}/{x}/{y}.png',
                params={'key': settings.twogis_api_key},
            )
        response.raise_for_status()
        if not response.content.startswith(b'\x89PNG\r\n\x1a\n') or len(response.content) > 1024 * 1024:
            raise ValueError('invalid tile response')
    except (httpx.HTTPError, ValueError):
        # Не выводим exception: он может содержать URL с ключом.
        _retry_at = time.monotonic() + 60
        raise TileUnavailable() from None
    _cache[key] = (now, response.content)
    _cache.move_to_end(key)
    while len(_cache) > MAX_TILES:
        _cache.popitem(last=False)
    return response.content
