"""Постраничный просмотр, отметки и переход к реальной метке Leaflet без внешней сети."""
import argparse
import asyncio
import json
from playwright.async_api import async_playwright
from tests.e2e.ui_regressions import PIXEL


async def scenario(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce')
    page=await context.new_page();errors=[];mutations=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:mutations.append(request.url) if request.method=='POST' else None)
    await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
    await page.route('https://tile.openstreetmap.org/**',lambda route:route.fulfill(body=PIXEL,content_type='image/png'))
    async def map_config(route):
        await asyncio.sleep(.25)
        await route.fulfill(json={'provider':'osm'})
    await page.route('**/api/map',map_config)
    await page.goto(base)
    await page.fill('#welcomeQuery','Казань, двухкомнатную до 9 млн рублей, наличными, только вторичка')
    await page.locator('#welcomeQuery').press('Enter')
    await page.get_by_role('button',name='Подтвердить рамки',exact=True).click(timeout=20000)
    await page.locator('.confirm-extras .actions').get_by_role('button',name='Пропустить',exact=True).click()
    await page.wait_for_function("document.querySelector('[data-pagination=top]')?.textContent.includes('из 30')")
    assert await page.locator('.offer').count()==10
    first=await page.locator('.offer').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.id)')
    await page.locator('.offer').first.locator('[data-vote=up]').click()
    before=len(mutations)
    nav=page.locator('[data-pagination=top]')
    await nav.get_by_role('button',name='Страница 2',exact=True).click()
    second=await page.locator('.offer').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.id)')
    assert len(second)==10 and not set(first)&set(second)
    assert await page.locator('.offer-no').first.inner_text()=='11'
    await page.locator('.offer').first.locator('[data-vote=up]').click()
    # Нажатие до завершения загрузки конфигурации карты ожидает её готовности.
    await page.locator('.offer').first.locator('[data-show-map]').click()
    await page.wait_for_function("document.querySelector('.flat-pin.is-selected') && document.querySelector('.leaflet-tooltip')")
    address=await page.locator('.offer .meta').first.inner_text()
    assert 'Тестовая улица' in await page.locator('.leaflet-tooltip').inner_text(),address
    assert await page.locator('.flat-pin.is-selected').count()==1
    assert await page.locator('[data-map]').evaluate('(node)=>document.activeElement===node')
    await page.screenshot(path=f'/tmp/selection-pages-{width}-paged.png')
    await nav.get_by_role('button',name='Страница 3',exact=True).click()
    assert await page.locator('.offer-no').first.inner_text()=='21'
    assert await nav.get_by_role('button',name='Следующая страница',exact=True).is_disabled()
    await nav.get_by_role('button',name='Страница 1',exact=True).click()
    assert await page.locator('.offer').first.locator('[data-vote=up]').get_attribute('aria-pressed')=='true'
    assert await page.locator('[data-liked-count]').inner_text()=='(2)'
    await nav.get_by_role('button',name='Страница 2',exact=True).click()
    assert await page.locator('.offer').first.locator('[data-vote=up]').get_attribute('aria-pressed')=='true'
    assert len(mutations)==before,'Просмотр страниц/карты не должен отправлять команды агенту'
    # Сообщение содержит точную страницу и ревизию; отметки включают другие страницы.
    received=[]
    async def message(route):
        received.append(json.loads(route.request.post_data))
        await route.fulfill(json={'handled_as':'question'})
    await page.route('**/api/chat/*/message',message)
    if not await page.locator('#query').is_visible():await page.locator('#chatToggle').click()
    await page.fill('#query','Среди этих квартир какая выгоднее?')
    await page.locator('#composer').evaluate('(form)=>form.requestSubmit()')
    await page.wait_for_function("document.querySelector('#query').value === ''")
    for _ in range(30):
        if received:break
        await asyncio.sleep(.05)
    assert received and received[0]['visible_listing_ids']==second
    assert len(received[0]['liked'])==2 and received[0]['selection_revision']>0
    # Новый короткий отбор сбрасывает страницу; нет координат — нет ложной метки.
    await page.evaluate("""async()=>{
      const {state}=await import('/js/state.js');
      const cards=state.results[4].cards.slice(0,3).map((c,i)=>i ? c : {...c,lat:null,lng:null});
      state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'result',step:4,kind:'selection',data:{...state.results[4],cards,selection_revision:state.results[4].selection_revision+1}})}));
    }""")
    assert await page.locator('.offer').count()==3
    assert await page.locator('[data-pagination=top] [data-selection-page]').count()==0
    assert await page.locator('.offer [data-show-map]').first.is_disabled()
    await page.locator('.offer [data-show-map]').nth(1).click()
    await page.wait_for_function("document.querySelector('.flat-pin.is-selected')")
    assert await page.locator('body').evaluate('(node)=>node.scrollWidth<=window.innerWidth')
    await page.screenshot(path=f'/tmp/selection-pages-{width}.png',full_page=True)
    assert not errors,errors
    await context.close()


async def main(base):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for width in (1440,390):
            await scenario(browser,base,width)
            print(f'PASS {width}px: three pages, likes, map focus, page-scoped message, short selection, missing coordinates',flush=True)
        await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8796')
    asyncio.run(main(parser.parse_args().base))
