"""Оболочка, карта и порядок запуска аудио без настоящих звонков.

Запуск против tests.e2e.server: python -m tests.e2e.ui_regressions --base http://127.0.0.1:8791
Аудио проверяется на поддельном AudioContext; тест не звонит и не доказывает работу iOS в фоне.
"""
import argparse
import asyncio
import base64

from playwright.async_api import async_playwright

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFz8AAAAASUVORK5CYII=')


async def main(base):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.goto(base, wait_until='domcontentloaded')
        await page.locator('.cookie-note button').click()
        assert await page.locator('#welcomePage').is_visible()
        assert await page.locator('[data-tab=economics]').count() == 0
        # Главная использует тот же настоящий bindMic, распознавание в тесте подменено.
        await page.evaluate("""async () => {
          const {recorder} = await import('/js/voice.js');
          window.savedListen = recorder.listen;
          recorder.available = recorder.supported = true;
          recorder.listen = () => ({stop(){}, result:Promise.resolve({text:'Хочу квартиру в Самаре до 10 млн'})});
        }""")
        await page.locator('#welcomeMicBtn').click()
        await page.wait_for_function("document.getElementById('welcomeQuery').value.includes('Самаре')")
        assert 'Проверьте текст' in await page.locator('#welcomeVoiceStatus').inner_text()
        await page.evaluate("""async () => { const {recorder} = await import('/js/voice.js'); recorder.listen = window.savedListen; }""")
        await page.locator('.site-nav [data-action=apartments]').click()
        await page.locator('[data-market=secondary]').click()
        await page.fill('#portalCity', 'Казань')
        await page.fill('#portalBudget', '9')
        await page.route('**/api/session', lambda route: route.fulfill(status=503, json={'detail': 'Тест передачи формы'}))
        async with page.expect_request('**/api/session') as sent:
            await page.locator('[data-action=portal-search]').click()
        query = (await sent.value).post_data_json['query']
        assert 'Казань' in query and '9 млн' in query and 'только вторичка' in query, query
        await page.wait_for_function('query => document.getElementById("query").value === query', arg=query)
        assert await page.locator('#sendBtn').is_enabled()
        await page.unroute('**/api/session')
        await page.goto(base, wait_until='domcontentloaded')
        await page.locator('.site-nav [data-action=smart-search]').click()
        await page.fill('#entryQuery', 'Двушка в Казани до 9 млн, наличными')
        await page.locator('#entryForm button[type=submit]').click()
        await page.get_by_role('button', name='Подтвердить рамки', exact=True).last.click(timeout=30_000)
        await page.wait_for_selector('[data-selection=proceed]:not([disabled])', timeout=30_000)
        await page.wait_for_selector('.leaflet-tile-loaded', timeout=15_000)
        assert not await page.locator('#chatPanel').evaluate("node => node.classList.contains('expanded')")
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert 'CARTO' not in await page.locator('.leaflet-control-attribution').inner_text()
        assert await page.locator('.leaflet-control-attribution svg').count() == 0
        # Реальный сигнал поиска меняет правую область, даже если старая подборка ещё видна.
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'search_progress',on:true,text:'Проверяю квартиры…'})}));
        }""")
        await page.wait_for_selector('#stage .search-spinner')
        assert await page.locator('#stage').get_attribute('aria-busy') == 'true'
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'search_progress',on:false})}));
        }""")
        await page.wait_for_selector('#stage .search-spinner', state='detached')
        await page.screenshot(path='/tmp/ui-mobile-selection.png', full_page=True)
        # Обычный текстовый вопрос модели должен снова открыть свёрнутый мобильный чат.
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({
            type:'message',role:'agent',text:'Продолжать поиск в этом бюджете?',await_reply:true
          })}));
        }""")
        await page.wait_for_selector('#chatPanel.expanded')
        await page.locator('#chatToggle').click()
        # Очередь модели видна и на телефоне; завершение ожидания убирает индикатор.
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'model_wait',on:true})}));
        }""")
        await page.wait_for_selector('#chatPanel.expanded')
        await page.wait_for_function("document.querySelector('.msg-thinking')?.textContent.includes('жду очередь')")
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'model_wait',on:false})}));
        }""")
        await page.wait_for_selector('.msg-thinking', state='detached')
        await page.locator('#chatToggle').click()
        print('PASS: новая главная → реальный запрос → подборка, мобильная ширина и локальный Leaflet')

        # Полная ошибка плиток должна давать ссылку на карту вместо немого пустого прямоугольника.
        await page.unroute('https://tile.openstreetmap.org/**')
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.abort())
        await page.evaluate("""async () => {
          const {mountMap}=await import('/js/map.js');
          const node=document.createElement('div');node.className='map';node.id='failure-map';
          document.getElementById('stage').append(node);
          await mountMap(node,[{id:'test',lat:55.796,lng:49.106,price:9000000,area:50,rooms:2,address:'Тестовый адрес'}]);
        }""")
        await page.wait_for_selector('#failure-map .map-error a', timeout=10_000)
        print('PASS: ошибка подложки видна пользователю, есть ссылка на внешнюю карту')

        # 2ГИС через локальный endpoint: сохранены маркеры, ключ в браузер не передаётся.
        await page.route('**/api/map', lambda route: route.fulfill(json={'provider': '2gis', 'tiles': '/api/map/tiles/{z}/{x}/{y}.png'}))
        await page.route('**/api/map/tiles/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.evaluate("""async () => {
          const {mountMap,unmountMap}=await import('/js/map.js');
          unmountMap(document.getElementById('failure-map'));
          document.getElementById('failure-map').remove();
          const node=document.createElement('div');node.className='map';node.id='twogis-map';
          document.getElementById('stage').append(node);
          await mountMap(node,[{id:'test',lat:55.796,lng:49.106,price:9000000,area:50,rooms:2,address:'Тест'}]);
        }""")
        await page.wait_for_selector('#twogis-map .leaflet-tile-loaded')
        assert '2ГИС' in await page.locator('#twogis-map .leaflet-control-attribution').inner_text()
        assert await page.locator('#twogis-map .flat-pin').count() == 1
        await page.unroute('**/api/map/tiles/**')
        await page.route('**/api/map/tiles/**', lambda route: route.abort())
        await page.unroute('https://tile.openstreetmap.org/**')
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.evaluate("document.querySelector('#twogis-map .leaflet-control-zoom-in').click()")
        await page.wait_for_selector('#twogis-map .map-error')
        await page.wait_for_function("document.querySelector('#twogis-map .leaflet-control-attribution').textContent.includes('OpenStreetMap')")
        assert await page.locator('#twogis-map .flat-pin').count() == 1
        print('PASS: 2ГИС с ценами квартир; при отказе сервиса подложка меняется на OSM')

        result = await page.evaluate("""async () => {
          const {PhoneCall}=await import('/js/phone.js');
          const {capturePhrase,prepareRecordingAudio}=await import('/js/voice.js');
          const original=window.AudioContext;
          const audioSession={type:'auto'};
          Object.defineProperty(navigator,'audioSession',{configurable:true,value:audioSession});
          const events=[];
          class FakeContext extends EventTarget {
            constructor(){ super();this.state='suspended';this.sampleRate=48000;events.push('create'); }
            resume(){events.push('resume');this.state='running';this.onstatechange?.();this.dispatchEvent(new Event('statechange'));return Promise.resolve();}
            createMediaStreamDestination(){return {stream:{kind:'outgoing'}};}
            createBuffer(){return {};}
            createAnalyser(){return {fftSize:1024,getFloatTimeDomainData(buf){buf.fill(0);},disconnect(){events.push('analyser-disconnect');}};}
            createMediaStreamSource(){return {connect(){},disconnect(){events.push('source-disconnect');}};}
            createBufferSource(){return {connect(){},start(){queueMicrotask(()=>this.onended?.());},stop(){this.onended?.();}};}
            decodeAudioData(){events.push('decode');return Promise.resolve({});}
            close(){this.state='closed';return Promise.resolve();}
          }
          window.AudioContext=FakeContext;
          const phone=new PhoneCall();
          if(phone.ctx!==null)throw new Error('AudioContext created before gesture');
          phone.connect=async()=>{events.push('connect');phone.ua={call:(number,options)=>{events.push('sip');queueMicrotask(options.eventHandlers.confirmed);return {isEnded:()=>true};},stop(){}};};
          const dial=phone.dial('79990000000');
          if(!events.includes('resume'))throw new Error('resume deferred beyond gesture');
          await dial;
          if(events.indexOf('resume')>events.indexOf('connect'))throw new Error('network before resume');
          if(audioSession.type!=='play-and-record')throw new Error('incorrect audio session');
          const originalFetch=window.fetch;
          window.fetch=async()=>new Response(new ArrayBuffer(8));
          phone.ctx.state='interrupted';phone.ctx.resume=()=>Promise.resolve();
          const speaking=phone.say('Тест');
          await new Promise(resolve=>setTimeout(resolve,30));
          if(events.includes('decode'))throw new Error('playing into interrupted context');
          phone.ctx.state='running';phone.ctx.dispatchEvent(new Event('statechange'));
          await speaking;
          const originalRecorder=window.MediaRecorder;
          window.MediaRecorder=class {
            static isTypeSupported(mime){return mime.startsWith('audio/mp4');}
            constructor(stream,options){this.mimeType=options.mimeType;}
            start(){this.state='recording';}
            stop(){this.state='inactive';queueMicrotask(()=>this.onstop());}
          };
          const recording=capturePhrase({}, {audioContext:phone.ctx});
          recording.stop();const recorded=await recording.result;
          if(!recorded.blob.type.startsWith('audio/mp4'))throw new Error('Safari AAC recording not supported');
          if(phone.ctx.state!=='running')throw new Error('capture closed shared call context');
          if(events.filter(e=>e==='create').length!==1)throw new Error('capture created another context');
          if(!events.includes('source-disconnect'))throw new Error('capture nodes leaked');
          phone.hangup();
          const microphone=prepareRecordingAudio();
          const phrase=capturePhrase({}, {audioContext:microphone.context});
          phrase.stop();await phrase.result;
          if(microphone.context.state!=='running')throw new Error('microphone context closed between phrases');
          microphone.release();
          if(microphone.context.state!=='closed')throw new Error('microphone context not released');
          window.MediaRecorder=originalRecorder;
          window.fetch=originalFetch;window.AudioContext=original;
          if(audioSession.type!=='auto')throw new Error('audio session not restored');
          return events;
        }""")
        print('PASS: Safari audio lifecycle:', result)
        assert not errors, errors
        await browser.close()
        print('ИТОГО: 4/4 группы проверок')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8791')
    asyncio.run(main(parser.parse_args().base))
