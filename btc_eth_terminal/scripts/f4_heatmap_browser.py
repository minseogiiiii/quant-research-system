"""Browser import and rejection test, synthetic projection only."""

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / "frontend"
fixture = dict(
    schema="f4_observed_depth_buckets_v1",
    mode="ARCHIVE_REPLAY",
    product="BTC-USD",
    source_manifest_sha256="a" * 64,
    epoch="a" * 64 + ":0",
    source_messages=4,
    independent_replay_status="INDEPENDENT_SOURCE_REPLAY_ONLY",
    definition="LAST_L2_OBSERVATION_PER_OCCUPIED_RECEIVER_BUCKET",
    coverage="ALL_OBSERVED_L2_LEVELS_INSIDE_REQUESTED_PRICE_WINDOW",
    live_feed=False,
    execution_authorized=False,
    zero_gap_certified=False,
    oos_approved=False,
    time_bucket_ms=1000,
    price_low="90",
    price_high_exclusive="120",
    price_width="10",
    missing_buckets_ms=[2000],
    rows=[
        dict(
            bucket_start_ms=1000,
            observed_receiver_ms=1500,
            source_sequence=2,
            best_bid="101",
            best_ask="110",
            cells=[
                dict(side="bid", price_low="100", quantity="0.5"),
                dict(side="offer", price_low="110", quantity="3"),
            ],
        )
    ],
)
with subprocess.Popen(
    ["npm", "exec", "--no", "--", "vite", "--config", "vite.heatmap.config.ts"],
    cwd=root,
) as server:
    try:
        url = "http://127.0.0.1:5176/heatmap.html"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("heatmap unavailable")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            page.get_by_text("No projection loaded", exact=False).wait_for()

            def upload(data):
                page.locator("input[type=file]").set_input_files(
                    dict(
                        name="fixture.json",
                        mimeType="application/json",
                        buffer=json.dumps(data).encode(),
                    )
                )

            upload(fixture)
            canvas = page.locator("canvas")
            canvas.wait_for()
            assert canvas.get_attribute("width") == "4"
            assert canvas.get_attribute("height") == "3"
            page.get_by_text("1 observed / 1 missing buckets", exact=False).wait_for()
            canvas.hover(position={"x": 10, "y": 10})
            page.get_by_text("Receiver observation:", exact=False).wait_for()
            page.get_by_label("Log intensity").uncheck()
            upload({**fixture, "live_feed": True})
            page.get_by_role("alert").wait_for()
            assert canvas.count() == 0
            browser.close()
        print("F4_HEATMAP_BROWSER_PASS")
    finally:
        server.terminate()
