"""Пять разнообразных живых диалогов. Проверяет смысл профиля и выдачи, без звонков/сделок.

Запуск: python -m tests.e2e.live_dialogue_cases --base https://... [--case slang]
Ошибочные ответы не заменяются дополнительными подсказками модели.
"""
import argparse
import asyncio
import contextlib
import json
import time

import httpx

CASES = {
    'slang': [
        'Казань. Не студию: однушку или двушку. Бюджет — пять с половиной лямов, наличка, только вторичка. Ремонт любой.',
    ],
    'disjoint': [
        'Казань, 1 или 3 комнаты, двухкомнатные не предлагайте. До 14 миллионов, наличными, вторичка. Ремонт любой.',
    ],
    'move_city': [
        'Москва или Подмосковье, однушка до 8 лямов, только вторичка, наличными. Ремонт любой.',
        'Передумал: только Казань. Бюджет, число комнат, оплату и вторичку оставь прежними. Москву и область больше не ищи.',
    ],
    'relax': [
        'Самара, 1–2 комнаты до 10 млн, наличными, вторичка. Парк желателен, но не обязателен. Первый и последний этаж не подходят. Ремонт любой.',
        'Парк больше не нужен. Первый этаж теперь можно, последний по-прежнему нельзя. Остальное сохрани.',
    ],
    'ambiguous_city': [
        'Хочу двушку в Новгороде до 9 лямов, вторичку, наличными. Ремонт любой.',
        'Великий Новгород, не Нижний. Остальные условия оставь прежними.',
    ],
}
PROFILE_KEYS = ('city', 'cities', 'rooms', 'rooms_options', 'budget_max', 'housing', 'payment',
                'poi', 'soft_criteria', 'not_first_floor', 'not_last_floor', 'condition', 'metro_max_minutes')


class Dialogue:
    def __init__(self, base):
        self.client = httpx.AsyncClient(base_url=base, timeout=190)
        self.events = asyncio.Queue()
        self.profile, self.cards, self.report, self.messages = {}, [], [], []
        self.reader = None
        self.sid = None

    async def start(self, query):
        response = await self.client.post('/api/session', json={'query': query})
        response.raise_for_status()
        self.sid = response.json()['id']
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            async with self.client.stream('GET', f'/api/chat/{self.sid}/stream') as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        self.events.put_nowait(json.loads(line[5:]))
            self.events.put_nowait({'type': 'transport_error', 'text': 'stream closed'})
        except Exception as exc:
            self.events.put_nowait({'type': 'transport_error', 'text': type(exc).__name__})

    async def send(self, text):
        self.cards = []
        response = await self.client.post(f'/api/chat/{self.sid}/message', json={'text': text})
        response.raise_for_status()
        return response.json()

    async def round(self):
        automated = []
        async with _timeout(175):
            while True:
                event = await self.events.get()
                kind = event.get('kind')
                if event['type'] in ('error', 'transport_error'):
                    return {'outcome': 'error', 'text': event.get('text'), 'automated': automated}
                if event['type'] == 'result':
                    if kind == 'profile':
                        self.profile = event['data']
                    elif kind == 'selection':
                        self.cards = event['data']['cards']
                    elif kind == 'search':
                        self.report = event['data'].get('report', [])
                if event['type'] == 'message' and event.get('role') == 'agent':
                    self.messages.append(event.get('text', ''))
                    if event.get('await_reply'):
                        return {'outcome': 'text_wait', 'text': event['text'], 'automated': automated}
                if event['type'] != 'confirm':
                    continue
                if kind in ('selection', 'clarify'):
                    return {'outcome': kind, 'text': event.get('text'), 'chips': event.get('chips'), 'automated': automated}
                # Все пять запросов явно задают оплату и рынок. Не меняем условия теста.
                data = {'red_lines': {}, 'payment': {'payment': 'cash'}, 'housing': {'housing': 'secondary'}}.get(kind)
                if data is None:
                    return {'outcome': 'unexpected_confirmation', 'kind': kind, 'automated': automated}
                automated.append(kind)
                response = await self.client.post(f'/api/session/{self.sid}/confirm', json={
                    'kind': kind, 'approved': True, 'data': data})
                response.raise_for_status()

    async def close(self):
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        await self.client.aclose()


@contextlib.asynccontextmanager
async def _timeout(seconds):
    # Python 3.10 совместимая граница ожидания.
    task = asyncio.current_task()
    timer = asyncio.get_running_loop().call_later(seconds, task.cancel)
    try:
        yield
    except asyncio.CancelledError:
        raise TimeoutError('Диалог не дошёл до результата за отведённое время') from None
    finally:
        timer.cancel()


def validate(name, phase, dialogue, outcome):
    p, cards = dialogue.profile, dialogue.cards
    errors = []
    def check(ok, message):
        if not ok:
            errors.append(message)
    if name == 'ambiguous_city' and phase == 0:
        check(outcome['outcome'] in ('clarify', 'text_wait'), 'Не уточнил неоднозначный Новгород')
        check(not p.get('city') and not cards, 'Сам выбрал город до уточнения клиента')
        return errors
    check(outcome['outcome'] == 'selection' and bool(cards), 'Не дошёл до подборки: ' + str(outcome))
    budgets = {'slang': 5_500_000, 'disjoint': 14_000_000, 'move_city': 8_000_000, 'relax': 10_000_000, 'ambiguous_city': 9_000_000}
    check(p.get('budget_max') == budgets[name], 'Неверно понял или изменил бюджет')
    check(p.get('payment') == 'cash' and p.get('housing') == 'secondary', 'Потерял оплату/рынок')
    check(not p.get('metro_max_minutes'), 'Добавил неназванное ограничение метро')
    check(p.get('condition') in (None, 'any'), 'Любой ремонт превратился в ограничение')
    allowed = {'slang': {1, 2}, 'disjoint': {1, 3}, 'move_city': {1}, 'relax': {1, 2}, 'ambiguous_city': {2}}[name]
    actual = set(p.get('rooms_options') or [p.get('rooms')])
    check(actual == allowed, f'Потерял допустимые комнатности: {actual}, ожидалось {allowed}')
    queried = {r.get('rooms') for r in dialogue.report if 'rooms' in r}
    check(allowed <= queried, f'Не запросил все допустимые комнатности: {queried}')
    check(all(x['rooms'] in allowed for x in cards), 'В выдаче запрещённая комнатность')
    check(all(x['price'] <= budgets[name] * 1.05 for x in cards), 'Цена вне действующего допуска поиска (+5% для торга)')
    if name in ('slang', 'disjoint'):
        check(p.get('city') == 'Казань', 'Неверный город')
    if name == 'move_city':
        territories = {p.get('city'), *(p.get('cities') or [])}
        expected = {'Москва', 'Московская область'} if phase == 0 else {'Казань'}
        check(territories == expected, f'Не обновил территории: {territories}')
        check(all(x['city'] in expected for x in cards), 'Квартиры из прежней территории')
    if name == 'relax':
        check(p.get('city') == 'Самара', 'Изменил город')
        check(bool(p.get('not_last_floor')) and 'not_last_floor' not in (p.get('soft_criteria') or []), 'Потерял запрет последнего этажа')
        check(all(not x.get('floors') or x.get('floor') != x['floors'] for x in cards), 'Показал известный последний этаж')
        if phase == 0:
            check(p.get('not_first_floor'), 'Не записал запрет первого этажа')
            check('park' in (p.get('poi') or []) and 'poi:park' in (p.get('soft_criteria') or []), 'Потерял желательность парка')
        else:
            check(not p.get('not_first_floor') and 'park' not in (p.get('poi') or []), 'Не снял отменённые условия')
    if name == 'ambiguous_city':
        check(p.get('city') == 'Великий Новгород', 'Потерял явное уточнение города')
        check(all(x['city'] == 'Великий Новгород' for x in cards), 'Выдача другого Новгорода')
    return errors


async def scenario(base, name):
    dialogue = Dialogue(base)
    failed = False
    try:
        await dialogue.start(CASES[name][0])
        for phase, text in enumerate(CASES[name]):
            started = time.monotonic()
            if phase:
                delivery = await dialogue.send(text)
            else:
                delivery = None
            outcome = await dialogue.round()
            errors = validate(name, phase, dialogue, outcome)
            failed |= bool(errors)
            cards = dialogue.cards
            print(json.dumps({
                'case': name, 'phase': phase + 1, 'session': dialogue.sid, 'ok': not errors,
                'seconds': round(time.monotonic() - started, 1), 'input': text, 'outcome': outcome,
                'profile': {k: dialogue.profile.get(k) for k in PROFILE_KEYS}, 'cards': len(cards),
                'card_rooms': sorted({x['rooms'] for x in cards}), 'card_cities': sorted({x['city'] for x in cards}),
                'first_floors': sum(x.get('floor') == 1 for x in cards),
                'last_floors': sum(bool(x.get('floors')) and x.get('floor') == x['floors'] for x in cards),
                'over_budget_for_bargain': sum(x['price'] > (dialogue.profile.get('budget_max') or 0) for x in cards),
                'reports': dialogue.report, 'delivery': delivery, 'errors': errors,
            }, ensure_ascii=False), flush=True)
        return failed
    finally:
        await dialogue.close()


async def main(args):
    failed = 0
    for name in [args.case] if args.case else CASES:
        try:
            failed += bool(await scenario(args.base.rstrip('/'), name))
        except Exception as exc:
            print(json.dumps({'case': name, 'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:250]}, ensure_ascii=False), flush=True)
            failed += 1
    return bool(failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--case', choices=CASES)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
