"""Живые GigaChat + Циан: создаёт диалоги, не звонит и не совершает денежных операций."""
import argparse
import asyncio
import json
import time

import httpx

CASES = {
    'samara': 'Ищем квартиру для бабушки в Самаре, 1 комната, бюджет 7–10 миллионов. Важны рядом парк и больница.',
    'center': 'Хочу 1–2 комнаты в Самаре за 10 млн, желательно в центре. Наличными, вторичка.',
    'region': 'Нужна однушка в Подмосковье до 10 млн, вторичка, наличными.',
    'both': 'Хочу 1-комнатную квартиру в Москве или Подмосковье до 10 млн, вторичка, наличными.',
}


async def scenario(base, name):
    async with httpx.AsyncClient(base_url=base, timeout=190) as client:
        response = await client.post('/api/session', json={'query': CASES[name]})
        response.raise_for_status()
        sid = response.json()['id']
        started = time.monotonic()
        cards, profile, confirms = [], {}, []
        async with client.stream('GET', f'/api/chat/{sid}/stream') as stream:
            async for line in stream.aiter_lines():
                if not line.startswith('data:'):
                    continue
                event = json.loads(line[5:])
                kind = event.get('kind')
                if event['type'] == 'error':
                    raise AssertionError('Ошибка агента')
                if event['type'] == 'message' and event.get('await_reply'):
                    raise AssertionError('Ожидание свободного ответа до первой подборки: ' + event['text'])
                if event['type'] == 'result':
                    if kind == 'profile':
                        profile = event['data']
                    if kind == 'selection':
                        cards = event['data']['cards']
                if event['type'] != 'confirm':
                    continue
                confirms.append(kind)
                if kind == 'selection':
                    assert cards, 'Пустая подборка'
                    if name == 'center':
                        assert profile.get('rooms_options') == [1, 2] and profile.get('prefer_center')
                    if name == 'region':
                        assert profile.get('city') == 'Московская область'
                    if name == 'both':
                        assert {'Москва', 'Московская область'} <= {profile.get('city'), *(profile.get('cities') or [])}
                    if name == 'samara':
                        assert {'park', 'hospital'} <= set(profile.get('poi') or [])
                    return {
                        'case': name, 'session': sid, 'seconds': round(time.monotonic() - started, 1),
                        'cards': len(cards), 'rooms': sorted({x['rooms'] for x in cards}),
                        'cities': sorted({x['city'] for x in cards}), 'confirmations': confirms,
                        'profile': {k: profile.get(k) for k in ('city', 'cities', 'rooms_options', 'prefer_center', 'poi')},
                        'top_ranking': cards[0].get('ranking'),
                    }
                data = {
                    'red_lines': {}, 'payment': {'payment': 'cash'}, 'housing': {'housing': 'both'},
                    'family': {'children': 0, 'matcap': 0},
                }.get(kind)
                assert data is not None, f'Неожиданная карточка: {kind}'
                response = await client.post(f'/api/session/{sid}/confirm', json={
                    'kind': kind, 'approved': True, 'data': data})
                response.raise_for_status()
        raise AssertionError('Лента завершилась без подборки')


async def main(args):
    failed = 0
    for name in [args.case] if args.case else CASES:
        try:
            result = await asyncio.wait_for(scenario(args.base.rstrip('/'), name), 180)
            print(json.dumps({'ok': True, **result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
            print(json.dumps({'ok': False, 'case': name, 'error': detail}, ensure_ascii=False), flush=True)
            failed += 1
    return bool(failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--case', choices=CASES)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
