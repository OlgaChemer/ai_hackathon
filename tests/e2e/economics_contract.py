"""Скрытый калькулятор загружается по ссылке и берёт допущения с backend."""

import argparse
import asyncio
from dataclasses import asdict
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright
from backend import economics


async def main(base):
    requests = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def respond(route):
            query = parse_qs(urlparse(route.request.url).query)
            defaults = economics.Inputs(fee_pct=0.22, fin_enabled=False)
            values = asdict(defaults)
            for key, value in query.items():
                values[key] = (
                    value[0] == "true" if key == "fin_enabled" else float(value[0])
                )
            inputs = economics.Inputs(**values)
            requests.append(query)
            await route.fulfill(
                json={
                    "defaults": asdict(defaults),
                    "unit": economics.unit(inputs),
                    "scenarios": economics.scenarios(defaults),
                }
            )

        await page.route("**/api/economics*", respond)
        await page.goto(base.rstrip("/") + "/#welcome")
        await page.wait_for_selector("#welcomeQuery")
        assert not requests, "Главная не должна загружать калькулятор экономики"
        await page.evaluate("location.hash='#economics'")
        fee = page.locator("#ec-fee_pct")
        await fee.wait_for()
        assert await fee.input_value() == "0.22"
        assert not await page.locator("[data-fin]").is_checked()
        await fee.fill("0.25")
        await fee.dispatch_event("input")
        await page.wait_for_function(
            "document.querySelector('#out-fee_pct').textContent.includes('25')"
        )
        await page.locator("[data-reset]").click()
        assert await fee.input_value() == "0.22"
        assert not errors, errors
        await browser.close()
        print(
            "PASS: экономика по прямой ссылке, загрузка по требованию, ставки с API и сброс к серверным допущениям"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    asyncio.run(main(parser.parse_args().base))
