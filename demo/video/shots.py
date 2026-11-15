# Read-only screenshots of the local Prova dashboard (GET only).
import asyncio, sys
from playwright.async_api import async_playwright
BASE = "http://127.0.0.1:8795/ui/"
PAGES = ["setup", "mission", "playbook", "lab", "owner", "dashboard"]
OUT = sys.argv[1]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1920, "height": 1080})
        for name in PAGES:
            try:
                await pg.goto(BASE + name + ".html", wait_until="networkidle", timeout=20000)
                await pg.wait_for_timeout(2500)
                await pg.screenshot(path=f"{OUT}/{name}.png")
                print("ok", name)
            except Exception as e:
                print("fail", name, e)
        await b.close()
asyncio.run(main())
