"""Регрессии кнопок подбора, порядка решений, гонки HTTP/SSE и программы звонка.

Запуск против tests.e2e.flexible_server; внешние звонки не выполняются.
"""
import argparse
import asyncio
from playwright.async_api import async_playwright
from tests.e2e.ui_regressions import PIXEL


async def scenario(browser,base,width):
    context=await browser.new_context(viewport={'width':width,'height':1000})
    page=await context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    await page.add_init_script("localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1')")
    await page.route('https://tile.openstreetmap.org/**',lambda route:route.fulfill(body=PIXEL,content_type='image/png'))
    await page.goto(base)
    await page.fill('#welcomeQuery','Казань, двухкомнатную до 9 млн рублей, наличными, только вторичка')
    await page.locator('#welcomeQuery').press('Enter')
    await page.get_by_role('button',name='Подтвердить рамки',exact=True).click(timeout=20000)
    await page.wait_for_function("document.querySelectorAll('.offer').length===10")
    await page.wait_for_function("!document.querySelector('[data-selection=more]').disabled")
    first=await page.locator('.offer').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.id)')
    for button in await page.locator('[data-vote=up]').all():
        await button.click()
    await page.locator('[data-selection=more]').click()
    await page.wait_for_function('(first)=>document.querySelectorAll(".offer").length===10 && [...document.querySelectorAll(".offer")].every(n=>!first.includes(n.dataset.id))',arg=first)
    await page.wait_for_function("!document.querySelector('[data-selection=more]').disabled")
    assert await page.locator('.msg-thinking').count()==0
    assert await page.locator('.msg-agent').filter(has_text='Отобрал для вас 10').count()>=1
    # Прогресс обновляет тот же узел; старый пустой SSE-результат не стирает карточки.
    await page.evaluate("""async()=>{
      const {state}=await import('/js/state.js');
      state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'search_progress',on:true,text:'Получено 2 реальных объявления'})}));
    }""")
    await page.wait_for_function("document.querySelector('[data-search-progress]')?.textContent.includes('Получено 2')")
    await page.evaluate("window.progressNode=document.querySelector('[data-search-progress]')")
    await page.evaluate("""async()=>{
      const {state}=await import('/js/state.js');
      state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'search_progress',on:true,text:'Получено 5 реальных объявлений'})}));
    }""")
    await page.wait_for_function("document.querySelector('[data-search-progress]')?.textContent.includes('Получено 5')")
    assert await page.evaluate("window.progressNode===document.querySelector('[data-search-progress]')")
    await page.evaluate("""async()=>{
      const {state}=await import('/js/state.js');
      state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'search_progress',on:false})}));
      state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'result',step:4,kind:'selection',data:{cards:[]}})}));
    }""")
    await page.wait_for_function("document.querySelector('#stage').textContent.includes('Сохранена предыдущая подборка')")
    assert await page.locator('.offer').count()==10
    # Старое решение остаётся до следующей реплики, устаревший resolved не закрывает новое.
    result=await page.evaluate("""async () => {
      const {state,chatLog,addMsg}=await import('/js/state.js');
      const {confirmCard,resolved}=await import('/js/cards.js');
      confirmCard({kind:'clarify',title:'Старый вопрос',text:'Уточнение',decision_id:'old'});
      const old=state.cards.clarify;
      const user=addMsg('user','Новый ответ');
      resolved({kind:'clarify',approved:null,decision_id:'old'});
      confirmCard({kind:'clarify',title:'Новый вопрос',text:'Следующее уточнение',decision_id:'new'});
      resolved({kind:'clarify',approved:true,decision_id:'old'});
      const next=state.cards.clarify;
      return {order:[...chatLog.children].indexOf(old)<[...chatLog.children].indexOf(user),
              current:next.dataset.decisionId,open:!next.classList.contains('is-done')};
    }""")
    assert result=={'order':True,'current':'new','open':True},result
    # Поздний HTTP-ответ не стирает новую карточку, успевшую прийти через SSE.
    await page.evaluate("""async () => {
      const {api}=await import('/js/api.js');
      const {confirmCard}=await import('/js/cards.js');
      api.chatMessage=async()=>{
        await new Promise(r=>setTimeout(r,100));
        confirmCard({kind:'clarify',title:'После отправки',text:'Жду человека',decision_id:'latest'});
        return {handled_as:'answer',continued:false};
      };
    }""")
    if not await page.locator('#query').is_visible():
        await page.locator('#chatToggle').click()
    await page.fill('#query','Ответ на предыдущий вопрос')
    await page.locator('#query').press('Enter')
    await page.wait_for_function("document.querySelector('.confirm[data-nonexistent]') || [...document.querySelectorAll('.confirm h4')].some(n=>n.textContent==='После отправки')")
    await page.wait_for_timeout(150)
    assert await page.evaluate("import('/js/state.js').then(m=>m.state.awaiting)")=='clarify'
    # Программа разговора видна и редактируется до любого звонка.
    text=await page.evaluate("""async () => {
      const {confirmCard}=await import('/js/cards.js');
      confirmCard({kind:'call',title:'Звонок',text:'Что ещё уточнить?',decision_id:'call-test',
        questions:[{question:'Есть ли перепланировки?'},{question:'Как со звукоизоляцией?'}]});
      return document.querySelector('[data-seller-questions]').value;
    }""")
    assert 'перепланировки' in text and 'звукоизоляцией' in text
    await page.locator('[data-seller-questions]').fill(text+'\nКогда освободите квартиру?')
    assert 'Когда освободите' in await page.locator('[data-seller-questions]').input_value()
    # Зависшая озвучка переговоров не задерживает новую подборку и её решения.
    await page.evaluate("""async () => {
      const {state,chatLog}=await import('/js/state.js');
      const {voice}=await import('/js/voice.js');
      const {createCall,renderCall}=await import('/js/call.js');
      const listing=state.results[4].cards[0];
      state.call=createCall({listing,fair:listing.fair,authority:listing.price});
      chatLog.append(renderCall(state.call,()=>{}));
      window.audioPending=true;
      voice.speak=()=>new Promise(resolve=>{window.finishTestSpeech=()=>{window.audioPending=false;resolve();};});
      const emit=ev=>state.es.onmessage(new MessageEvent('message',{data:JSON.stringify(ev)}));
      emit({type:'turn',speaker:'agent',text:'Проверяю детали квартиры'});
      emit({type:'result',step:4,kind:'selection',data:{...state.results[4],cards:[listing]}});
      emit({type:'confirm',kind:'selection',decision_id:'audio-independent',title:'Обновлённый выбор',text:'Выберите квартиру'});
    }""")
    await page.wait_for_function("document.querySelectorAll('.offer').length===1 && !document.querySelector('[data-selection=more]').disabled")
    assert await page.evaluate('window.audioPending'), 'Проверка не застала ожидающую озвучку'
    await page.evaluate('window.finishTestSpeech()')
    assert not errors,errors
    await page.screenshot(path=f'/tmp/flexible-ui-{width}.png',full_page=False)
    await context.close()


async def main(base):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for width in (1440,390):
            await scenario(browser,base,width)
            print(f'PASS {width}px: more/likes, decision order, stale ID, HTTP/SSE race, seller questions')
        await browser.close()


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--base',default='http://127.0.0.1:8796')
    asyncio.run(main(ap.parse_args().base))
