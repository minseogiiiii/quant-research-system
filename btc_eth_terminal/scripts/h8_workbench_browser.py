"""Actual backend fixture -> local qualification tab -> fail-closed import."""

import hashlib
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from h2_native_benchmark import generate
from h1_streaming_parquet import convert
from h6_registered_strategy_runner import run
from h5_experiment_registry import sha
from h8_qualification import assess

root = Path(__file__).resolve().parents[1] / "frontend"
with tempfile.TemporaryDirectory() as d:
    p = Path(d)
    pin, *_ = generate(p / "raw", 2_000_000, levels=8)
    convert(p / "raw", pin, p / "dataset", fixture=True)
    entry = dict(
        dataset_path=str(p / "dataset"),
        dataset_receipt_sha256=hashlib.sha256(
            (p / "dataset" / "dataset.json").read_bytes()
        ).hexdigest(),
        role="DEVELOPMENT",
    )
    journal = p / "trial.db"
    r = run(
        [entry], 0, [{"product": "BTC-USD", "hold_ms": 5000}], 42, journal, fixture=True
    )
    receipt = assess(r, journal, r["plan_sha256"], r["registry_head_sha256"], sha(r))
with subprocess.Popen(
    ["npm", "exec", "--no", "--", "vite", "--config", "vite.microstructure.config.ts"],
    cwd=root,
) as server:
    try:
        url = "http://127.0.0.1:5178/microstructure.html"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url, timeout=1) as res:
                    if res.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Workbench unavailable")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            requests = []
            page.route(
                "**/q292/**",
                lambda route: (requests.append(route.request.url), route.abort()),
            )
            page.goto(url)
            page.get_by_role("button", name="QUALIFICATION", exact=True).click()
            page.get_by_text("No admitted qualification report.").wait_for()

            def upload(x):
                page.locator("input[type=file]").set_input_files(
                    dict(
                        name="qualification.json",
                        mimeType="application/json",
                        buffer=json.dumps(x).encode(),
                    )
                )

            upload(receipt)
            page.get_by_role("heading", name="NO APPROVAL", exact=True).wait_for()
            page.get_by_role("cell", name="BLOCKED", exact=True).wait_for()
            page.get_by_text("Synthetic engineering fixture", exact=True).wait_for()
            page.get_by_text("Same-policy holdout not verified", exact=True).wait_for()
            upload(dict(receipt, execution_authorized=True))
            page.get_by_role("alert").wait_for()
            assert (
                page.get_by_role("heading", name="NO APPROVAL", exact=True).count() == 0
            )
            assert page.get_by_role("cell", name="BLOCKED", exact=True).count() == 0
            assert requests == []
            browser.close()
        print("H8_WORKBENCH_BROWSER_PASS")
    finally:
        server.terminate()
