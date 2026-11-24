"""Isolated browser UI contract; no real feed or trading authorization."""
import subprocess
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / "frontend"
with subprocess.Popen(["npm", "exec", "--no", "--", "vite", "--config", "vite.workspace.config.ts"], cwd=root) as server:
    try:
        url = "http://127.0.0.1:5175/workspace.html"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("workspace unavailable")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.route("**/q292/**", lambda route: route.abort())
            page.goto(url)
            page.get_by_text("No currently trusted quote").first.wait_for()
            page.get_by_role("button", name="Resize BTC", exact=True).click()
            assert page.locator('[data-widget="BTC"]').get_attribute("class") == "wide"
            page.reload()
            assert page.locator('[data-widget="BTC"]').get_attribute("class") == "wide"
            page.get_by_role("button", name="Move ETH earlier", exact=True).click()
            assert page.locator("article").first.get_attribute("data-widget") == "ETH"
            page.get_by_role("button", name="Commands (Ctrl/⌘ K)").click()
            page.get_by_role("textbox", name="Command").fill("BUY BTC")
            page.get_by_role("button", name="Run", exact=True).click()
            page.get_by_text("Unknown command; no action executed").wait_for()
            page.get_by_role("textbox", name="Command").fill("BTC OFI")
            page.get_by_role("button", name="Run", exact=True).click()
            page.locator('[data-widget="BTC OFI"]').wait_for()
            page.get_by_role("button", name="Risk", exact=True).click()
            page.get_by_text("No qualified widgets connected", exact=False).wait_for()
            assert page.locator("article").count() == 0
            browser.close()
        print("F3_BROWSER_PASS (blocked feed, persistence, reorder, allowlist, unconnected view)")
    finally:
        server.terminate()
