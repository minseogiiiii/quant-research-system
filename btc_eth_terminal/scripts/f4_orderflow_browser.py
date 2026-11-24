"""Synthetic sealed archive -> Python projection -> browser reconciliation."""
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from test_f4_order_flow_archive import produce

root=Path(__file__).resolve().parents[1]/"frontend"
with tempfile.TemporaryDirectory() as d:
    report,_=produce(Path(d))
with subprocess.Popen(["npm","exec","--no","--","vite","--config","vite.orderflow.config.ts"],cwd=root) as server:
    try:
        url="http://127.0.0.1:5177/orderflow.html"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url,timeout=1) as response:
                    if response.status==200:
                        break
            except OSError:
                time.sleep(.5)
        else:
            raise RuntimeError("orderflow unavailable")
        with sync_playwright() as p:
            browser=p.chromium.launch()
            page=browser.new_page()
            page.goto(url)
            page.get_by_text("No order flow projection loaded.").wait_for()
            def upload(data):
                page.locator('input[type=file]').set_input_files(dict(name="fixture.json",mimeType="application/json",buffer=json.dumps(data).encode()))
            upload(report)
            page.get_by_text("4 unique updates",exact=False).wait_for()
            page.get_by_text("0.100000000000000001",exact=True).wait_for()
            page.get_by_label("Observed bucket").select_option("2")
            page.get_by_text("CVD 0.250000000000000001",exact=False).wait_for()
            assert page.locator("tbody tr").count()==1
            report["rows"][0]["cells"][0]["buy_base"]="0.1"
            upload(report)
            page.get_by_role("alert").wait_for()
            assert page.locator("svg").count()==0
            browser.close()
        print("F4_ORDERFLOW_ARCHIVE_BROWSER_PASS")
    finally:
        server.terminate()
