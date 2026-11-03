"""Размер переписки и независимая прокрутка рабочего экрана без вызовов LLM."""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main(base, shots):
    folder=Path(shots) if shots else None
    if folder:folder.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        try:
            for width,height in ((1440,900),(1366,768),(1024,768),(390,844)):
                context=await browser.new_context(viewport={'width':width,'height':height})
                await context.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
                page=await context.new_page()
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(base.rstrip('/')+'/#agent',wait_until='domcontentloaded')
                await page.wait_for_selector('#account button',state='attached')
                await page.wait_for_function("() => import('/js/state.js').then(({config})=>config.cities?.featured?.length>0)")
                await page.evaluate('document.fonts.ready')
                await page.evaluate("""async () => {
                    const {addMsg}=await import('/js/state.js');
                    for(let i=0;i<8;i++)addMsg(i%2?'agent':'user', 'Сообщение '+(i+1)+': ищем квартиру рядом с парком, учитываем бюджет и сравниваем подходящие варианты.');
                }""")
                chat=await page.locator('#chatPanel').bounding_box()
                log=await page.locator('#chatLog').bounding_box()
                composer=await page.locator('#composer').bounding_box()
                assert abs(chat['height']-height)<=1 and abs(chat['y'])<=1,(width,chat)
                assert log['height']>=height*.5,(width,log)
                assert composer['y']+composer['height']<=height,(width,composer)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('#chatLog').evaluate('n=>n.scrollHeight>n.clientHeight')
                if folder:await page.screenshot(path=str(folder/f'{width}x{height}-chat.png'))
                if width>860:
                    assert abs(chat['x']+chat['width']-width)<=1
                    header=await page.locator('.site-header').bounding_box()
                    assert header['x']+header['width']<=chat['x']+1
                    for button in await page.locator('.site-nav button, #account button, #voiceToggle').all():
                        box=await button.bounding_box()
                        assert box['x']+box['width']<=chat['x']+1,(width,box,chat)
                    stage=await page.locator('section.stage').bounding_box()
                    assert stage['x']+stage['width']<=chat['x']+1
                    await page.evaluate("""() => {
                        const content=document.createElement('div');content.style.height='2000px';
                        document.getElementById('stage').append(content);
                        document.querySelector('section.stage').scrollTop=400;
                    }""")
                    assert await page.locator('section.stage').evaluate('n=>n.scrollTop')>=400,await page.locator('section.stage').evaluate('n=>({height:n.clientHeight,scroll:n.scrollHeight,top:n.scrollTop})')
                    after=await page.locator('#chatPanel').bounding_box()
                    assert abs(after['y']-chat['y'])<1 and abs(after['height']-chat['height'])<1
                assert not errors,errors
                print(f'PASS {width}x{height}: чат {chat["height"]:.0f}px, переписка {log["height"]:.0f}px, поле ввода в окне',flush=True)
                await context.close()
        finally:await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',required=True)
    parser.add_argument('--shots',default='')
    args=parser.parse_args()
    asyncio.run(main(args.base,args.shots))
