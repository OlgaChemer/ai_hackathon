"""Выбранная квартира без общего бюджета: предметный лимит, затем ипотечная анкета."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for width in (1440,390):
            page=await browser.new_page(viewport={'width':width,'height':1000});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
            await page.goto('http://127.0.0.1:8796')
            await page.fill('#welcomeQuery','Москва, трешка')
            await page.locator('#welcomeQuery').press('Enter')
            await page.get_by_role('button',name='Без ограничений',exact=True).last.click()
            await page.wait_for_function("document.querySelector('.offer [data-choose]') && !document.querySelector('.offer [data-choose]').disabled")
            price=await page.evaluate("async()=>{const {state}=await import('/js/state.js');return state.results[4].cards[0].price}")
            await page.locator('.offer [data-choose]').first.click()
            card=page.locator('.confirm-red_lines:not(.is-done)');await card.wait_for()
            assert 'Лимит по выбранной квартире' in await card.inner_text()
            assert int(await card.locator('[data-max-range]').input_value())==price
            await card.get_by_role('button',name='Подтвердить рамки',exact=True).click()
            await page.get_by_role('button',name='Ипотека, взнос ещё копим',exact=True).click()
            family=page.locator('.confirm-family:not(.is-done)');await family.wait_for()
            await family.locator('[name=children]').select_option('0')
            await family.get_by_role('button',name='Подобрать программу',exact=True).click()
            await page.locator('.confirm-deposit:not(.is-done)').wait_for()
            assert await page.locator('.confirm-clarify:not(.is-done)').count()==0
            state=await page.evaluate("async()=>{const {state}=await import('/js/state.js');return {budget:state.results[1].budget_max,liked:state.marks.liked.size}}")
            assert state=={'budget':price,'liked':1},state
            assert not errors,errors
            print(f'PASS {width}px: selected price confirmed, mortgage family leads to deposit without restarting intake',flush=True)
            await page.close()
        await browser.close()


if __name__=='__main__':asyncio.run(main())
