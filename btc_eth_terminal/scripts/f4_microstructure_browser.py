"""Generated sealed archive -> two projections -> synchronized isolated modes."""
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from f4_depth_buckets import build as depth_build
from f4_order_flow import build as flow_build
from test_f4_order_flow_archive import source
from test_f4_depth_buckets_archive import BASE

root=Path(__file__).resolve().parents[1]/"frontend"
with tempfile.TemporaryDirectory() as d:
    archive=Path(d)
    pin,_=source(archive)
    args=dict(product="BTC-USD",start_ms=BASE,end_ms=BASE+4000,bucket_ms=1000)
    depth=depth_build(archive,pin,**args,low="90",high="120",width="10")
    flow=flow_build(archive,pin,**args,price_width="1")
with subprocess.Popen(["npm","exec","--no","--","vite","--config","vite.microstructure.config.ts"],cwd=root) as server:
    try:
        url="http://127.0.0.1:5178/microstructure.html"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url,timeout=1) as response:
                    if response.status==200:
                        break
            except OSError:
                time.sleep(.5)
        else:
            raise RuntimeError("workspace unavailable")
        with sync_playwright() as p:
            browser=p.chromium.launch()
            page=browser.new_page()
            requests=[]
            def blocked(route):
                requests.append(route.request.url)
                route.fulfill(status=503,content_type="application/json",body="{}")
            page.route("**/q292/**",blocked)
            page.goto(url)
            page.get_by_text("No admitted projection pair.").wait_for()
            assert requests==[]
            def upload(depth_data,flow_data):
                for i,data in enumerate((depth_data,flow_data)):
                    page.locator('input[type=file]').nth(i).set_input_files(dict(name="projection.json",mimeType="application/json",buffer=json.dumps(data).encode()))
                page.get_by_role("button",name="Validate pair").click()
            upload(depth,flow)
            page.get_by_text("4 aligned buckets",exact=False).wait_for()
            assert page.locator("canvas").count()==1
            page.get_by_role("combobox",name="Receiver-time bucket",exact=True).select_option(str(BASE+2000))
            page.get_by_text("No observed L2 in this bucket").wait_for()
            page.get_by_text("No observed unique trade updates in this bucket").wait_for()
            page.get_by_role("combobox",name="Receiver-time bucket",exact=True).select_option(str(BASE+3000))
            page.get_by_text("CVD 0.250000000000000001",exact=False).wait_for()
            assert requests==[]
            invalid={**flow,"source_manifest_sha256":"b"*64,"epoch":"b"*64+":0"}
            upload(depth,invalid)
            page.get_by_role("alert").wait_for()
            assert page.locator("canvas").count()==0
            upload(depth,flow)
            page.locator("canvas").wait_for()
            page.get_by_role("button",name="LIVE READ-ONLY",exact=True).click()
            page.get_by_role("heading",name="Research Workspace").wait_for()
            assert page.locator("canvas").count()==0
            for _ in range(50):
                if requests:
                    break
                page.wait_for_timeout(100)
            assert requests and all("/q292/" in r for r in requests)
            page.get_by_role("button",name="ARCHIVE REPLAY",exact=True).click()
            page.get_by_text("No admitted projection pair.").wait_for()
            assert page.get_by_role("heading",name="Research Workspace").count()==0
            total=len(requests)
            page.wait_for_timeout(1500)
            assert len(requests)==total
            browser.close()
        print("F4_MICROSTRUCTURE_BROWSER_PASS")
    finally:
        server.terminate()
