"""Премиум UI, три задачи, скачивание и отказ вне тематики на desktop/mobile."""
import argparse
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright


async def login(page,email):
    await page.get_by_role('button',name='Войти',exact=True).click()
    await page.locator('dialog.auth input[name=email]').fill(email)
    await page.locator('dialog.auth input[name=password]').fill('workshop-ui-only-123')
    await page.locator('dialog.auth button[value=submit]').click()
    await page.locator('dialog.auth').wait_for(state='detached')


async def main(base):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for width in (1440,390):
            context=await browser.new_context(viewport={'width':width,'height':1000})
            page=await context.new_page();errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
            await page.goto(base)
            assert not await page.locator('#workshopButton').is_visible()
            assert (await page.request.get(base+'/api/workshop/context')).status==401
            await login(page,'darkyhat@mail.ru')
            await page.locator('#workshopButton').click()
            await page.locator('[data-context]').filter(has_text='понравившихся квартир: 2').wait_for()
            assert await page.locator('dialog.workshop input[type=file]').count()==0
            for kind in ('repair','compare','market'):
                await page.locator('[data-kind='+kind+']').click()
                if kind=='market':await page.locator('[data-wide]').check()
                await page.locator('[data-start]').click()
                await page.locator('.workshop-report').wait_for()
                await page.locator('[data-start]').wait_for(state='visible')
                await page.wait_for_function("!document.querySelector('[data-start]').disabled")
                assert await page.locator('.workshop-facts table tr').count()>=2
                csv=await page.request.get(base+(await page.locator('[data-result] a').filter(has_text='Скачать CSV').get_attribute('href')))
                assert csv.status==200
            await page.locator('[data-kind=repair]').click()
            await page.locator('[data-prompt]').fill('Напиши шахматную игру для квартиры')
            await page.locator('[data-start]').click()
            await page.locator('[data-error]').filter(has_text='Я помогаю с недвижимостью').wait_for()
            await page.screenshot(path=f'/tmp/workshop-ui-{width}.png',full_page=False)
            assert await page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'Горизонтальная прокрутка страницы'
            assert not errors,errors
            await page.locator('[data-close]').click()
            await context.close()
            context=await browser.new_context(viewport={'width':width,'height':1000});page=await context.new_page()
            await page.goto(base);await login(page,'other@example.ru')
            assert not await page.locator('#workshopButton').is_visible()
            assert (await page.request.get(base+'/api/workshop/context')).status==403
            await context.close()
            print(f'PASS {width}px: premium-only, three jobs, downloads, off-topic refusal, no uploads',flush=True)
        await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8798')
    asyncio.run(main(parser.parse_args().base))
