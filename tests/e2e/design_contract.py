"""Дизайн ux/ui сохраняет запуск поиска, данные форм, голос и доступ к разделам.

Запуск против tests.e2e.server: python -m tests.e2e.design_contract --base http://127.0.0.1:8794
Модель и объявления тестовые; реальные микрофон и звонки не используются.
"""
import argparse
import asyncio
import base64
from pathlib import Path

from playwright.async_api import async_playwright

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFz8AAAAASUVORK5CYII=')


async def voice_reply(page, text):
    await page.evaluate("""async text => {
      const {talk} = await import('/js/voicechat.js');
      await talk.onText(text);
    }""", text)


async def scenario(browser, base, width, shots):
    context = await browser.new_context(viewport={'width': width, 'height': 900})
    await context.add_init_script("localStorage.setItem('voice','0'); localStorage.setItem('cookieNotice','1')")
    page = await context.new_page()
    errors, posts = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: posts.append((request.url, request.post_data_json))
            if request.method == 'POST' else None)
    await page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
    await page.goto(base, wait_until='domcontentloaded')
    await page.fill('#portalCity', 'Казань')
    await page.fill('#portalBudget', '9')
    await page.locator('[data-action=portal-search]').click()
    red_lines = page.locator('#chatLog .confirm-red_lines')
    await red_lines.wait_for()
    assert len([url for url, body in posts if url.endswith('/api/session')]) == 1
    assert not await page.locator('#entryDialog').evaluate('node => node.open')
    assert await page.locator('#stage .confirm, #stage .facts').count() == 0
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    # История возвращает активное подтверждение на прежнее место, с тем же диалогом.
    sid = await page.evaluate("location.hash")
    await page.reload(wait_until='domcontentloaded')
    await red_lines.wait_for()
    assert await page.evaluate('location.hash') == sid
    assert len([url for url, body in posts if url.endswith('/api/session')]) == 1
    await red_lines.locator('[data-budget]').fill('8800000')
    if shots:
        await page.screenshot(path=str(shots / f'{width}-confirmation.png'))
    await voice_reply(page, 'да')
    await page.locator('#chatLog .confirm-payment').wait_for()
    confirmed = next(body for url, body in posts if url.endswith('/confirm') and body['kind'] == 'red_lines')
    assert confirmed['approved'] is True and confirmed['data']['budget_max'] == 8_800_000
    assert confirmed['data']['_said'] == 'да'
    await page.locator('#chatLog .confirm-red_lines.is-done').wait_for(state='attached')
    assert await page.locator('#chatLog .confirm-red_lines button:not(:disabled)').count() == 0
    await page.locator('#chatLog .confirm-red_lines .decision-archive summary').click()
    archived_budget = page.locator('#chatLog .confirm-red_lines [data-budget]')
    assert await archived_budget.is_visible() and await archived_budget.is_disabled()
    assert await archived_budget.input_value() == '8800000'

    await voice_reply(page, 'копим на взнос')
    family = page.locator('#chatLog .confirm-family')
    await family.wait_for()
    await family.locator('[name=children]').select_option('1')
    await family.locator('[name=youngest_age]').fill('4')
    await family.locator('[name=has_matcap]').check()
    await family.locator('[name=matcap]').fill('737200')
    await family.get_by_role('button', name='Подобрать программу', exact=True).click()
    family_body = next(body for url, body in posts if url.endswith('/confirm') and body['kind'] == 'family')
    assert {key: family_body['data'][key] for key in ('children', 'youngest_age', 'matcap')} == {
        'children': 1, 'youngest_age': 4, 'matcap': 737200}

    finance = page.locator('#chatLog .confirm-finance')
    await finance.wait_for()
    await finance.locator('[name=monthly_income]').fill('250000')
    await finance.locator('[name=monthly_expenses]').fill('120000')
    await finance.locator('[name=savings]').fill('1500000')
    await finance.get_by_role('button', name='Рассчитать стратегию', exact=True).click()
    await page.wait_for_selector('[data-selection=proceed]:not(:disabled)', timeout=30_000)
    finance_body = next(body for url, body in posts if url.endswith('/confirm') and body['kind'] == 'finance')
    assert finance_body['data']['savings'] == 1_500_000
    assert finance_body['data']['monthly_income'] == 250_000
    assert finance_body['data']['monthly_expenses'] == 120_000
    assert await page.locator('#chatLog .confirm:not(.is-done)').count() == 0
    assert await page.locator('#chatLog .confirm-finance.is-done').count() == 1

    # Навигация только меняет представление: никаких новых команд агенту.
    count = len(posts)
    if width < 860:
        await page.locator('#chatToggle').click()
    await page.wait_for_selector('.loan-option')
    assert await page.locator('.loan-option').count() == 3
    assert await page.locator('[data-loan-choice], [data-send-loan], [data-edit-finance]').count() == 0
    assert await page.evaluate("""async () => {
      const {state} = await import('/js/state.js');
      const {rub} = await import('/js/format.js');
      return state.results[2].finance.inputs.savings === 1500000
        && document.querySelector('.loan-option .big').textContent.startsWith(rub(state.results[2].mortgage.scenarios[0].monthly));
    }""")
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if shots:
        await page.screenshot(path=str(shots / f'{width}-finance.png'))
    assert await page.locator('[data-tab=economics]').count() == 0
    assert await page.locator('#stage .loan-option, #stage .facts, #stage .confirm').count() == 0
    if width < 860:
        await page.locator('#chatToggle').click()
    assert await page.locator('[data-selection=proceed]').is_enabled()
    assert len(posts) == count
    if shots:
        await page.screenshot(path=str(shots / f'{width}-selection.png'))

    # Таблица поясняет уже полученные признаки, не меняя подборку и расчёты.
    await page.evaluate("""async () => {
      const {state} = await import('/js/state.js');
      const {selection} = await import('/js/views.js');
      const card = structuredClone(state.results[4].cards[0]);
      card.match = {status:'unverified', unknown:['парк'], checks:[
        {key:'poi:park', label:'парк', status:null, required:true, evidence:'Расстояние не подтверждено'},
        {key:'feature:balcony', label:'балкон', status:true, required:false, evidence:'<script>unsafe</script>'}
      ]};
      const host = document.createElement('div'); host.id = 'criteria-preview';
      host.innerHTML = selection({...state.results[4], cards:[card]}, state.marks);
      document.getElementById('stage').append(host);
    }""")
    await page.locator('#criteria-preview summary').click()
    detail = await page.locator('#criteria-preview table').inner_text()
    assert 'Нет данных' in detail and 'Подтверждено' in detail and 'пожелание' in detail
    assert await page.locator('#criteria-preview script').count() == 0
    # Ответ текстом может пометить уточнение завершённым до прихода resolved.
    # Даже в этом порядке раскрытый архив не должен повторно отправлять подсказку.
    assert await page.evaluate("""async () => {
      const {state} = await import('/js/state.js');
      const {confirmCard, resolved} = await import('/js/cards.js');
      confirmCard({kind:'clarify', title:'Уточнение', text:'Какой город?', chips:['Москва'], replay:true});
      state.cards.clarify.classList.add('is-done');
      resolved({kind:'clarify', approved:null});
      const archived = document.querySelector('#chatLog .confirm-clarify');
      return archived.querySelector('.decision-archive') && archived.querySelector('[data-quick]').disabled;
    }""")
    assert not errors, errors
    await context.close()
    print(f'PASS {width}px: формы/голос, история, финансы в чате, квартиры справа и навигация; API сохранён', flush=True)


async def main(base, shots):
    folder = Path(shots) if shots else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for width in (1440, 390):
                await scenario(browser, base, width, folder)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8794')
    parser.add_argument('--shots', default='')
    args = parser.parse_args()
    asyncio.run(main(args.base, args.shots))
