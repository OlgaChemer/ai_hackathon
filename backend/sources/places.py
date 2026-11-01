"""2GIS для инфраструктуры. Ошибка/неполный ответ означает неизвестность, а не отсутствие.

Результаты нормализуются к формату geo.py; общий бюджет и запасной OSM задаёт geo.nearby.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from ..config import settings

URL = 'https://catalog.api.2gis.com/3.0/items'
RUBRICS = {'school': '245', 'kindergarten': '237', 'park': '168'}
_retry_at = 0.0
_last_provider = 'auto'


def status() -> dict:
    return {'configured': bool(settings.twogis_api_key),
            'provider': _last_provider if settings.twogis_api_key else 'osm',
            'retry_in': max(0, round(_retry_at - time.monotonic()))}


async def enrich(listings: list[dict], kinds: list[str], radius: int) -> None:
    """До четырёх одновременных запросов. Неподдержанные виды останутся для OSM."""
    global _retry_at, _last_provider
    if not settings.twogis_api_key or time.monotonic() < _retry_at:
        return
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=6) as client:
        async def one(x: dict, kind: str):
            global _retry_at, _last_provider
            async with sem:
                if time.monotonic() < _retry_at:
                    return
                params = {'rubric_id': RUBRICS[kind], 'point': f"{x['lng']},{x['lat']}",
                          'radius': radius, 'key': settings.twogis_api_key, 'page_size': 10,
                          'sort': 'distance', 'fields': 'items.point,items.name'}
                try:
                    response = await client.get(URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                    meta = data.get('meta') or {}
                    if meta.get('code') != 200 or not isinstance(data.get('result'), dict):
                        raise ValueError('invalid catalog response')
                    result = data['result']
                    items = result.get('items', [])
                    if not isinstance(items, list) or type(result.get('total')) is not int or result['total'] < 0:
                        raise ValueError('incomplete catalog response')
                    # Не превращаем недоступную геометрию/неполную страницу в «рядом нет».
                    if result['total'] and not items:
                        raise ValueError('missing catalog items')
                    from ..geo import distance_m
                    candidates = []
                    for item in items:
                        point = item.get('point') or {}
                        lat, lng = float(point['lat']), float(point['lon'])
                        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                            raise ValueError('invalid coordinates')
                        dist = distance_m(x['lat'], x['lng'], lat, lng)
                        candidates.append({'name': str(item.get('name') or kind), 'dist': dist,
                                           'lat': lat, 'lng': lng})
                    found = min(candidates, key=lambda p: p['dist']) if candidates else None
                    # Для парка точка каталога не доказывает отсутствие близкой границы.
                    # Далёкий центр парка проверит OSM по контуру.
                    if kind == 'park' and found and found['dist'] > radius:
                        return
                    if found and found['dist'] > radius:
                        return
                    x.setdefault('nearby', {})[kind] = found
                    x.setdefault('nearby_evidence', {})[kind] = {
                        'source': '2gis', 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'distance_kind': 'straight_line', 'radius': radius}
                    _last_provider = '2gis'
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    # Не логируем URL: ключ передаётся в query string.
                    _retry_at = time.monotonic() + 60
                    _last_provider = 'osm'
        tasks = [one(x, k) for x in listings for k in kinds
                 if k in RUBRICS and k not in (x.get('nearby') or {})]
        await asyncio.gather(*tasks)
