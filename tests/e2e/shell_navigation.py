"""Смысл навигации и сохранение подбора: desktop/mobile, настоящий тестовый API."""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def check(browser, base, width, folder):
    context = await browser.new_context(viewport={'width':width, 'height':900})
    await context.add_init_script("localStorage.setItem('voice','0'); localStorage.setItem('cookieNotice','1')")
    page = await context.new_page()
    errors, starts = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: starts.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/session') else None)
    await page.goto(base, wait_until='domcontentloaded')
    nav = page.locator('.site-nav')
    await page.wait_for_selector('#account button')
    assert await page.locator('.site-header').count() == 1

    # Поиск по параметрам и свободный запрос имеют разные входы.
    await nav.locator('[data-action=apartments]').click()
    assert await page.locator('#portalForm').is_visible()
    assert not await page.locator('#entryDialog').evaluate('node => node.open')
    await nav.locator('[data-action=smart-search]').click()
    await page.fill('#entryQuery', 'Квартира с парком рядом')
    await page.press('#entryQuery', 'Escape')
    assert not starts
    await nav.locator('[data-action=smart-search]').click()
    assert await page.input_value('#entryQuery') == 'Квартира с парком рядом'
    await page.locator('[data-action=close-entry]').click()
    await nav.locator('[data-action=new-buildings]').click()
    assert await page.locator('[data-market=new]').get_attribute('aria-pressed') == 'true'
    await nav.locator('[data-action=apartments]').click()
    assert await page.locator('[data-market=both]').get_attribute('aria-pressed') == 'true'

    # Аккаунт доступен в общей шапке без открытия пустого окна агента.
    await page.locator('[data-account=login]').click()
    assert await page.locator('dialog.auth').is_visible()
    assert await page.locator('#aggregatorHome').is_visible()
    await page.locator('dialog.auth [value=cancel]').click()
    if folder:
        await page.screenshot(path=str(folder / f'{width}-home.png'))

    # Enter/повторная отправка формы создают ровно один подбор.
    await page.fill('#portalCity', 'Казань')
    await page.fill('#portalBudget', '9.25')
    await page.evaluate("document.getElementById('portalForm').requestSubmit(); document.getElementById('portalForm').requestSubmit()")
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    assert len(starts) == 1
    sid = await page.evaluate("location.hash.slice(6)")
    assert starts[0]['query'].endswith('новостройки и вторичка')
    assert '9.25 млн' in starts[0]['query']
    if width<860:await page.locator('#chatToggle').click()
    assert await page.locator('#currentSearchLink').is_visible()

    # История — самостоятельная страница, диалог продолжает ждать своё решение.
    await nav.locator('[data-action=open-chats]').click()
    await page.wait_for_selector(f'[data-history-chat="{sid}"]')
    assert await page.locator('#historyPage').is_visible()
    assert not await page.locator('#agentApp').is_visible()
    assert await page.locator('#account button').is_visible()
    await page.fill('#historyQuery', 'Несуществующий запрос')
    await page.get_by_role('button', name='Сбросить фильтры', exact=True).wait_for()
    await page.locator('[data-history-reset]').click()
    await page.select_option('#historyStatus', 'active')
    assert await page.locator(f'[data-history-chat="{sid}"]').count() == 1
    await page.select_option('#historyStatus', 'completed')
    assert await page.locator('[data-history-chat]').count() == 0
    await page.select_option('#historyStatus', 'all')
    if folder:
        await page.screenshot(path=str(folder / f'{width}-history.png'))

    # Новый умный поиск не теряет старый подбор до отправки запроса.
    await nav.locator('[data-action=smart-search]').click()
    assert await page.input_value('#entryQuery') == ''  # отправленный черновик очищен
    await page.fill('#entryQuery', 'Трёшка с тихим двором')
    await page.locator('[data-action=close-entry]').click()
    assert await page.locator('#historyPage').is_visible()
    assert len(starts) == 1

    # Прямая ссылка на историю переживает перезагрузку, открывает прежний диалог.
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_selector(f'[data-history-chat="{sid}"]')
    await page.locator(f'[data-history-chat="{sid}"]').click()
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    assert await page.evaluate('location.hash') == '#chat-' + sid
    assert len(starts) == 1
    if width<860 and await page.locator('#chatToggle').get_attribute('aria-expanded')=='true':
        await page.locator('#chatToggle').click()
    await nav.locator('[data-action=apartments]').click()
    await page.go_back()
    await page.wait_for_selector('#agentApp', state='visible')
    assert await page.evaluate('location.hash') == '#chat-' + sid
    await page.go_forward()
    await page.wait_for_selector('#aggregatorHome', state='visible')

    # Ошибка списка не выглядит пустой историей, повтор восстанавливает данные.
    await page.route('**/api/chats', lambda route: route.fulfill(status=503, json={'detail':'temporary'}))
    await nav.locator('[data-action=open-chats]').click()
    await page.wait_for_selector('[data-history-retry]')
    await page.unroute('**/api/chats')
    await page.locator('[data-history-retry]').click()
    await page.wait_for_selector(f'[data-history-chat="{sid}"]')
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    for button in await nav.locator('button').all():
        box = await button.bounding_box()
        assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
    assert len(starts) == 1

    # Новый запрос из текущего диалога создаёт другой подбор и сохраняет путь назад.
    await page.locator('#currentSearchLink').click()
    if width<860 and await page.locator('#chatToggle').get_attribute('aria-expanded')=='true':
        await page.locator('#chatToggle').click()
    await nav.locator('[data-action=smart-search]').click()
    await page.fill('#entryQuery', 'Двушка в Москве до 12 млн, наличными')
    await page.locator('#entryForm [type=submit]').click()
    await page.wait_for_function('(old) => location.hash.startsWith("#chat-") && location.hash !== old', arg='#chat-' + sid)
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    new_sid = await page.evaluate('location.hash')
    assert len(starts) == 2
    await page.go_back()
    await page.wait_for_function('(expected) => location.hash === expected', arg='#chat-' + sid)
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    await page.go_forward()
    await page.wait_for_function('(expected) => location.hash === expected', arg=new_sid)
    assert len(starts) == 2 and not errors, errors
    await context.close()
    print(f'PASS {width}px: разные входы, история/фильтры/ошибка, новый подбор и возврат без лишних запусков', flush=True)


async def main(base, shots):
    folder = Path(shots) if shots else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for width in (1440, 390):
                await check(browser, base, width, folder)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8798')
    parser.add_argument('--shots', default='')
    args = parser.parse_args()
    asyncio.run(main(args.base, args.shots))
