"""Озвучка сумм, настройки голоса, перебивание и свободный ответ вместо случайного согласия."""

import argparse
import asyncio
from playwright.async_api import async_playwright

MOCK_SPEECH = r"""(() => {
  window.__spoken = [];
  class Utterance {constructor(text) {this.text=text;}}
  window.SpeechSynthesisUtterance = Utterance;
  const synth = new EventTarget();
  synth.getVoices = () => [
    {voiceURI:'test-ru',name:'Русский голос 1',lang:'ru-RU',default:true},
    {voiceURI:'test-ru-2',name:'Русский голос 2',lang:'ru-RU'},
    {voiceURI:'test-en',name:'English',lang:'en-US'}];
  synth.speak = u => {
    window.__spoken.push({text:u.text,rate:u.rate,pitch:u.pitch,voice:u.voice?.voiceURI});
    if (!window.__holdSpeech) setTimeout(()=>u.onend?.(), 10);
  };
  synth.cancel = () => {}; // Safari может не прислать onend после отмены.
  Object.defineProperty(window,'speechSynthesis',{value:synth,configurable:true});
  localStorage.setItem('voice','1');localStorage.setItem('cookieNotice','1');
})();"""


async def scenario(browser, base, width):
    context = await browser.new_context(
        viewport={"width": width, "height": 900}, permissions=["microphone"]
    )
    await context.add_init_script(MOCK_SPEECH)
    page = await context.new_page()
    errors = []
    posts = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            posts.append((request.url, request.post_data_json))
            if request.method == "POST"
            else None
        ),
    )
    await page.goto(base)
    # Отмена должна завершить ожидание сразу даже без onend, без таймера в десятки секунд.
    assert await page.evaluate("""async () => {
      const {voice}=await import('/js/voice.js');
      window.__holdSpeech=true;
      const speaking=voice.speak('42 000 000 ₽','agent',true);
      voice.stop();
      const stopped=await Promise.race([speaking.then(()=>true),new Promise(r=>setTimeout(()=>r(false),150))]);
      window.__holdSpeech=false;return stopped;
    }""")
    await page.evaluate(
        "import('/js/voice.js').then(({recorder})=>{recorder.available=true})"
    )
    await page.locator("#welcomeTalkBtn").click()
    await page.locator("#talkPanel").wait_for(state="visible")
    await page.locator(".talk-settings summary").click()
    assert (
        await page.locator("#talkVoice option").count() == 3
    )  # auto и только два русских
    await page.select_option("#talkVoice", "test-ru-2")
    await page.fill("#talkRate", "1.1")
    await page.locator("#talkRate").dispatch_event("input")
    text_box = await page.locator(".talk-text").bounding_box()
    hint_box = await page.locator("#talkHint").bounding_box()
    settings_box = await page.locator(".talk-settings").bounding_box()
    orb_box = await page.locator("#talkOrb").bounding_box()
    panel_box = await page.locator("#talkPanel").bounding_box()
    assert text_box["width"] >= 110, text_box
    assert hint_box["height"] < 120, hint_box
    assert settings_box["y"] >= orb_box["y"] + orb_box["height"], (settings_box, orb_box)
    assert panel_box["height"] <= 450, panel_box
    await page.locator("#talkSample").click()
    await page.wait_for_function(
        "window.__spoken.some(s=>s.text.includes('сорок два миллиона рублей')&&s.voice==='test-ru-2'&&s.rate===1.1&&s.pitch===1)"
    )
    await page.evaluate(
        "import('/js/voicechat.js').then(({talk})=>talk.onText('Казань, двушка до 42 млн, наличными'))"
    )
    await page.locator(".confirm-red_lines:not(.is-done)").wait_for()
    assert (
        next(data for url, data in posts if url.endswith("/api/session"))["voice_mode"]
        is True
    )
    before = sum("/confirm" in url for url, _ in posts)
    phrase = "Да, но бюджет до 40 млн, парк желателен."
    await page.evaluate(
        "text=>import('/js/voicechat.js').then(({talk})=>talk.onText(text))", phrase
    )
    assert sum("/confirm" in url for url, _ in posts) == before, (
        "Свободный ответ не должен нажимать согласие"
    )
    assert any(
        url.endswith("/message")
        and data.get("text") == phrase
        and data.get("voice_mode") is True
        for url, data in posts
    )
    # Отрицание в свободной реплике у подборки не является командой «звони».
    await page.evaluate("""async () => {
      document.querySelectorAll('.confirm').forEach(n=>n.classList.add('is-done'));
      const {state}=await import('/js/state.js');state.awaiting='selection';
    }""")
    assert await page.evaluate("""async () => {
      const {state}=await import('/js/state.js');
      const previous=state.results[4];state.results[4]={cards:[{id:'voice-first'},{id:'voice-second'}]};
      const {talk}=await import('/js/voicechat.js');await talk.onText('вторая нравится');
      const liked=state.marks.liked.has('voice-second');
      state.marks.liked.delete('voice-second');state.results[4]=previous;return liked;
    }""")
    phrase = "Не звони продавцу, сначала найди рядом парк."
    await page.evaluate(
        "text=>import('/js/voicechat.js').then(({talk})=>talk.onText(text))", phrase
    )
    assert sum("/confirm" in url for url, _ in posts) == before
    assert any(
        url.endswith("/message") and data.get("text") == phrase for url, data in posts
    )
    await page.locator("#talkEnd").click()
    assert await page.evaluate("import('/js/api.js').then(({api})=>!api.voiceMode)")
    assert await page.evaluate(
        "import('/js/voicechat.js').then(({talk})=>!talk.on&&!talk.stream)"
    )
    assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    # Настройки сохраняются между открытиями страницы, режим сам не включается.
    await page.reload()
    await page.wait_for_selector("#talkVoice option", state="attached")
    assert await page.evaluate(
        "import('/js/voice.js').then(({voice})=>voice.voiceId==='test-ru-2'&&voice.rate===1.1)"
    )
    assert not errors, errors
    await context.close()
    print(
        f"PASS {width}px: суммы словами, голос/темп, перебивание, свободный ответ без согласия, сохранение настроек",
        flush=True,
    )


async def main(base):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ]
        )
        try:
            for width in (1440, 390):
                await scenario(browser, base, width)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    asyncio.run(main(parser.parse_args().base))
