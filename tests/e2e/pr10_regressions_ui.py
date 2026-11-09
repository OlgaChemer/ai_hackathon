"""Карточки семьи/лимита и выбор одной квартиры на изолированном flexible_server."""
import argparse
import asyncio
from playwright.async_api import async_playwright
from tests.e2e.ui_regressions import PIXEL


async def card_regressions(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':1000})
    page=await context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
    await page.goto(base,wait_until='domcontentloaded')
    await page.evaluate('''async()=>{
        const {showAgent}=await import('/js/shell.js');showAgent();
        const {state}=await import('/js/state.js');state.sid='card-regression';
        const {api}=await import('/js/api.js');
        window.reviewPosts=[];
        api.confirm=async(sid,kind,approved,data)=>{window.reviewPosts.push({kind,approved,data});return {ok:true};};
        const {confirmCard}=await import('/js/cards.js');
        window.showReviewCard=confirmCard;
        confirmCard({kind:'family',title:'Семья',text:'Проверка',replay:false,decision_id:'family-no',
            defaults:{children:1,youngest_age:4,matcap:0,matcap_default:737200,matcap_known:true}});
    }''')
    family=page.locator('.confirm-family:not(.is-done)')
    assert await family.locator('[data-cap-choice=no]').evaluate("b=>b.classList.contains('is-on')")
    assert not await family.locator('[data-cap-amount]').is_visible()
    await family.locator('.actions button').first.click()
    assert (await page.evaluate('window.reviewPosts.at(-1)'))['data']['matcap']==0
    await page.evaluate('''()=>window.showReviewCard({kind:'family',title:'Семья',text:'Проверка',replay:false,
        decision_id:'family-yes',defaults:{children:1,youngest_age:4,matcap:0,matcap_default:737200}})''')
    family=page.locator('.confirm-family:not(.is-done)')
    await family.locator('[data-cap-choice=yes]').click()
    assert await family.locator('[data-cap-amount]').is_visible()
    await family.locator('[name=matcap]').fill('500000')
    assert await family.locator('[data-cap-choice=yes]').evaluate("b=>b.classList.contains('is-on')")
    await family.locator('.actions button').first.click()
    assert (await page.evaluate('window.reviewPosts.at(-1)'))['data']['matcap']==500000
    for maximum,minimum in [(8760000,6100000),(800000,500000),(15000000,0),(27000000,0)]:
        await page.evaluate('''([maximum,minimum])=>window.showReviewCard({kind:'red_lines',title:'Границы',
            text:'Проверка',replay:false,decision_id:'budget-'+maximum,
            payload:{city:'Казань',rooms:2,budget_max:maximum,budget_min:minimum,step:500000,floor:1000000}})''',
            [maximum,minimum])
        budget=page.locator('.confirm-red_lines:not(.is-done)')
        assert int(await budget.locator('[data-max-range]').input_value())==maximum
        if maximum==27000000:
            assert int(await budget.locator('[data-min-range]').input_value())==18900000
            assert '30%' in await budget.locator('[data-range-hint]').inner_text()
        await budget.locator('.actions button').first.click()
        values=(await page.evaluate('window.reviewPosts.at(-1)'))['data']
        assert (values['budget_max'],values['budget_min'])==(maximum,max(minimum,(maximum*7+9)//10))
    await page.evaluate('''()=>window.showReviewCard({kind:'red_lines',title:'Границы',text:'Проверка',replay:false,
        payload:{city:'Казань',rooms:2,budget_max:8760000,budget_min:6100000,step:500000,floor:1000000}})''')
    budget=page.locator('.confirm-red_lines:not(.is-done)');upper=budget.locator('[data-max-range]')
    await upper.focus();await upper.press('ArrowRight')
    assert int(await upper.input_value())==9000000
    assert int(await budget.locator('[data-min-range]').input_value())==6300000
    await upper.press('ArrowRight');assert int(await upper.input_value())==9500000
    assert int(await budget.locator('[data-min-range]').input_value())==6650000
    await page.evaluate('''()=>{
        const input=document.querySelector('.confirm-red_lines:not(.is-done) [data-min-range]');
        input.value='0';input.dispatchEvent(new Event('input',{bubbles:true}));
    }''')
    assert int(await budget.locator('[data-min-range]').input_value())==6650000
    await page.evaluate('''()=>{
        const input=document.querySelector('.confirm-red_lines:not(.is-done) [data-min-range]');
        input.value='12000000';input.dispatchEvent(new Event('input',{bubbles:true}));
    }''')
    assert int(await budget.locator('[data-min-range]').input_value())<=int(await upper.input_value())
    await page.evaluate('''async()=>{
        const {setChatExpanded}=await import('/js/shell.js');setChatExpanded(false);
        const {finance}=await import('/js/views.js');
        document.getElementById('stage').innerHTML=finance({mode:'needs_deposit',note:'Взнос пока не указан'});
    }''')
    assert 'Взнос пока не указан' in await page.locator('#stage').inner_text()
    assert not errors,errors
    print(f'PASS {width}px: explicit matcap, exact price limits, keyboard, deferred mortgage',flush=True)
    await context.close()


async def specific_call(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':1000})
    page=await context.new_page();errors=[];confirmations=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda r:confirmations.append(r.post_data_json)
        if r.method=='POST' and r.url.endswith('/confirm') else None)
    await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
    await page.route('https://tile.openstreetmap.org/**',lambda route:route.fulfill(body=PIXEL,content_type='image/png'))
    await page.goto(base,wait_until='domcontentloaded')
    await page.fill('#welcomeQuery','Казань, двухкомнатную до 9 млн рублей, наличными, только вторичка')
    await page.locator('#welcomeQuery').press('Enter')
    await page.get_by_role('button',name='Подтвердить рамки',exact=True).click(timeout=20000)
    await page.locator('.confirm-extras .actions').get_by_role('button',name='Пропустить',exact=True).click()
    await page.wait_for_function("document.querySelectorAll('.offer').length>=3 && !document.querySelector('[data-selection=more]').disabled")
    offers=page.locator('.offer')
    previous=await offers.nth(0).get_attribute('data-id')
    target=await offers.nth(2).get_attribute('data-id')
    await offers.nth(0).locator('[data-vote=up]').click()
    await offers.nth(2).locator('[data-choose]').click()
    await page.locator('.confirm-call:not(.is-done)').wait_for(timeout=20000)
    decision=next(r for r in confirmations if r['kind']=='selection')
    assert decision['data']['listing_id']==target
    assert set(decision['data']['liked'])=={previous,target}
    saved=await page.evaluate('''async()=>{
        const {state}=await import('/js/state.js');return {likes:[...state.marks.liked],event:state.cards.call.decisionEvent};
    }''')
    assert set(saved['likes'])=={previous,target}
    assert target in str(saved['event']),saved['event']
    assert not errors,errors
    print(f'PASS {width}px: real selection confirmation calls one chosen listing and keeps favorites',flush=True)
    await context.close()


async def main(base):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for width in (1440,390):
            await card_regressions(browser,base,width)
            await specific_call(browser,base,width)
        await browser.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8796')
    asyncio.run(main(parser.parse_args().base))
