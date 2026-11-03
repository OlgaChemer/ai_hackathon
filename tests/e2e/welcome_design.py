"""Новый стартовый экран: отправка, примеры, история и видимость поля сообщения."""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def check(browser, base, width, folder):
    context = await browser.new_context(viewport={'width': width, 'height': 900})
    await context.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
    page = await context.new_page()
    starts, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: starts.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/session') else None)
    await page.goto(base, wait_until='domcontentloaded')
    await page.wait_for_selector('#account button')
    assert await page.locator('#welcomePage').is_visible()
    assert await page.locator('#welcomeMicBtn').is_visible()
    assert await page.locator('.site-nav [data-action=open-chats]').is_visible()
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if folder:
        await page.screenshot(path=str(folder / f'{width}-welcome.png'))

    # Короткая фраза не запускает поиск; двойная отправка создаёт один диалог.
    await page.fill('#welcomeQuery', 'да')
    await page.evaluate("document.getElementById('welcomeForm').requestSubmit()")
    assert not starts
    query = 'Двушка в Казани до 9 млн, наличными'
    await page.fill('#welcomeQuery', query)
    await page.evaluate("document.getElementById('welcomeForm').requestSubmit();document.getElementById('welcomeForm').requestSubmit()")
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    assert starts == [{'query': query}]
    assert await page.input_value('#welcomeQuery') == ''
    old_hash = await page.evaluate('location.hash')
    if width > 860:
        chat, stage = await page.locator('#chatPanel').bounding_box(), await page.locator('.stage').bounding_box()
        assert stage['x'] + stage['width'] <= chat['x'] + 1
    composer = await page.locator('#composer').bounding_box()
    assert composer and composer['y'] >= 0 and composer['y'] + composer['height'] <= 900
    if folder:
        await page.screenshot(path=str(folder / f'{width}-chat.png'))

    # Возврат на главную и в историю сохраняет предыдущий подбор.
    await page.locator('[data-action=home]').click()
    await page.wait_for_selector('#welcomePage', state='visible')
    await page.locator('.site-nav [data-action=open-chats]').click()
    await page.locator(f'[data-history-chat="{old_hash[6:]}"]').wait_for()
    assert len(starts) == 1
    await page.locator('[data-action=home]').click()

    # Пример отправляется целиком и ровно один раз.
    example = page.locator('.welcome-examples [data-welcome-example]').last
    expected = await example.get_attribute('data-welcome-example')
    await example.click()
    await page.wait_for_function('(old) => location.hash.startsWith("#chat-") && location.hash !== old', arg=old_hash)
    await page.wait_for_selector('#chatLog .confirm-red_lines')
    assert len(starts) == 2 and starts[-1] == {'query': expected}
    assert not errors, errors
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    await context.close()
    print(f'PASS {width}px: старт, микрофон, примеры, история, чат справа и поле сообщения в экране', flush=True)


async def main(args):
    folder = Path(args.shots) if args.shots else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for width in (1440, 390):
                await check(browser, args.base.rstrip('/'), width, folder)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--shots', default='')
    asyncio.run(main(parser.parse_args()))
