"""Анонимные подборы переходят в аккаунт — в браузере (Chromium) против tests.e2e.server.

  .venv/bin/python -m tests.e2e.server --port 8790 &
  .venv/bin/python -m tests.e2e.auth_flow --base http://127.0.0.1:8790

Три анонимных подбора → регистрация → все три в аккаунте и на втором устройстве, каждый открывается
с перепиской → после выхода браузер их не видит.
"""
import argparse
import asyncio
import time

from playwright.async_api import async_playwright

B = "http://127.0.0.1:8790/"
QUERIES = ["Двушка в Казани до 9 млн, наличными", "Студия в Москве до 12 млн", "Трёшка в Екатеринбурге до 10 млн"]
R = []
def ok(name, cond, extra=""):
    R.append(bool(cond)); print(("✓" if cond else "✗"), name, extra, flush=True)
async def chats(pg): return await pg.evaluate("fetch('/api/chats').then(r=>r.json()).then(d=>d.chats.map(c=>c.title))")
async def wait_count(pg, n):
    for _ in range(80):
        if len(await chats(pg)) >= n: return True
        await asyncio.sleep(0.5)
    return False
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(); pg = await ctx.new_page(); errs=[]
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(B); await asyncio.sleep(1.5)
        for i, q in enumerate(QUERIES):
            if i == 0:
                await pg.click("[data-action=smart-search]"); await asyncio.sleep(0.8)
                await pg.fill("#welcomeQuery", q); await pg.locator("button:visible", has_text="Начать подбор").last.click()
            else:
                await pg.click("#chatsBtn"); await pg.click("[data-new-chat]"); await asyncio.sleep(0.8)
                await pg.fill("#query", q); await pg.click("#sendBtn")
            await wait_count(pg, i + 1); await asyncio.sleep(1.5)
        ok("3 анонимных подбора в браузере", sorted(await chats(pg)) == sorted(QUERIES), str(await chats(pg)))
        await pg.click("[data-account=login]"); await pg.click("dialog.auth [data-switch]")
        email = f"multi{int(time.time())}@ex.ru"
        await pg.fill("dialog.auth input[name=email]", email); await pg.fill("dialog.auth input[name=password]", "hackathon2026")
        await pg.click('dialog.auth [value="submit"]'); await asyncio.sleep(1.5)
        toast = await pg.evaluate("document.querySelector('.account-toast')?.innerText")
        ok("уведомление о переносе 3 подборов", toast and "3 подбора" in toast, toast)
        ok("после регистрации в аккаунте все 3", sorted(await chats(pg)) == sorted(QUERIES))
        # второе устройство
        ctx2 = await b.new_context(); pg2 = await ctx2.new_page(); pg2.on("pageerror", lambda e: errs.append(str(e)))
        await pg2.goto(B); await asyncio.sleep(1.5)
        ok("второе устройство до входа — пусто", await chats(pg2) == [])
        await pg2.click("[data-account=login]")
        await pg2.fill("dialog.auth input[name=email]", email); await pg2.fill("dialog.auth input[name=password]", "hackathon2026")
        await pg2.click('dialog.auth [value="submit"]'); await asyncio.sleep(1.5)
        ok("второе устройство после входа — все 3", sorted(await chats(pg2)) == sorted(QUERIES), str(await chats(pg2)))
        await pg2.click("[data-action=open-chats]"); await asyncio.sleep(1.5)
        n = await pg2.locator("[data-history-chat]").count()
        ok("страница «История подбора» показывает 3", n == 3, f"{n}")
        ids = await pg2.evaluate("fetch('/api/chats').then(r=>r.json()).then(d=>d.chats.map(c=>[c.id,c.title]))")
        for cid, title in ids:
            await pg2.goto(B + f"#chat-{cid}"); await pg2.reload(); await asyncio.sleep(2.5)
            log = await pg2.evaluate("document.querySelector('#chatLog')?.innerText || ''")
            ok(f"открывается с перепиской: {title[:28]}", title in log)
        # выход на первом — там пусто
        await pg.click("[data-account=menu]"); await pg.click("[data-logout]"); await asyncio.sleep(1)
        ok("после выхода на первом устройстве подборы скрыты", await chats(pg) == [])
        ok("без ошибок в консоли", not errs, "; ".join(errs[:3]))
        await b.close()
    print(f"ИТОГО: {sum(R)}/{len(R)}")
    return 0 if all(R) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=B)
    B = ap.parse_args().base.rstrip("/") + "/"
    raise SystemExit(asyncio.run(main()))
