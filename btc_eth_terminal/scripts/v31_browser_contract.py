#!/usr/bin/env python3
"""Historical terminal browser contract against actual local readonly backend data.

The host may prohibit Chromium URL navigation. We deliberately use Chromium DOM,
real compiled React/Funnest dist and the exact standalone JS, mediating fetches
through Python's loopback HTTP client. This is NOT a true browser network E2E test.
No fixture market prices, credentials or live orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def source_http(port: int, calls: list[str]):
    def _fetch(url: str):
        if not isinstance(url, str) or not url.startswith("/api/") or len(url) > 1200:
            raise RuntimeError("Browser requested unsupported backend operation")
        calls.append(url)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}" + url, timeout=8
            ) as r:
                return {"status": r.status, "ok": True, "json": json.loads(r.read())}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "ok": False, "json": json.loads(e.read())}

    return _fetch


def equip(page, bridge_fn):
    page.expose_function("__v31Backend", bridge_fn)
    # Wrapping in an IIFE prevents Playwright from treating the assignment
    # expression as a callback to be invoked with an implicit null argument.
    page.evaluate("""(() => {window.fetch=async (url) => {
      const r=await window.__v31Backend(String(url));
      return {ok:r.ok,status:r.status,json:async()=>r.json};
    }})()""")


def run(output: Path, dist: Path):
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    port = free_port()
    calls = []
    checks = []
    errors = []
    server = subprocess.Popen(
        [sys.executable, "backend/bridge.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "QUANT_BRIDGE_PORT": str(port)},
    )

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    try:
        time.sleep(0.6)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get("V31_CHROMIUM", "/usr/bin/chromium"),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                # Standalone dependency-free browser, including actual HTTP
                # response transport bridged by urllib and a deliberate race.
                page = browser.new_page(viewport={"width": 1380, "height": 900})
                page.on("pageerror", lambda e: errors.append(str(e)))
                html = (
                    (ROOT / "frontend/standalone/index.html")
                    .read_text()
                    .replace('<link rel="stylesheet" href="/terminal/styles.css">', "")
                    .replace('<script src="/terminal/app.js" defer></script>', "")
                )
                page.set_content(html)
                page.add_style_tag(
                    content=(ROOT / "frontend/standalone/styles.css").read_text()
                )
                equip(page, source_http(port, calls))
                page.add_script_tag(
                    content=(ROOT / "frontend/standalone/app.js").read_text()
                )
                page.wait_for_function(
                    "document.querySelector('#source-status')?.textContent==='SOURCE-AUDITED'",
                    timeout=12000,
                )
                check(
                    "standalone_real_archive_BTC_11_bars",
                    page.locator("#candle-count").inner_text().startswith("11"),
                )
                check(
                    "standalone_chart_canvas",
                    page.locator("canvas").evaluate("(c)=>c.width>200&&c.height>200"),
                )
                page.locator('button[data-product="ETH-USD"]').click()
                page.wait_for_function(
                    "document.querySelector('#breadcrumb-product')?.textContent==='ETH-USD'",
                    timeout=12000,
                )
                check(
                    "standalone_eth_switch",
                    page.locator("#candle-count").inner_text().startswith("11"),
                )
                page.locator("#asof").select_option(index=1)
                page.wait_for_function(
                    (
                        "document.querySelector('#rep"
                        "lay-state')?.textContent==='"
                        "RECEIVER AS-OF'"
                    ),
                    timeout=12000,
                )
                check(
                    "standalone_completed_bar_cutoff",
                    page.locator("#candle-count").inner_text().startswith("0"),
                )
                page.locator('button[data-view="evidence"]').click()
                check(
                    "standalone_real_manifest_sha",
                    len(page.locator("#source-sha").inner_text()) == 64,
                )
                page.locator('button[data-view="paper"]').click()
                check(
                    "standalone_paper_not_armed",
                    "SYNTHETIC FIXTURE ONLY"
                    in page.locator("#view-paper").inner_text(),
                )
                # Delay stale ETH responses so they arrive after a newer BTC request.
                page.evaluate("""(() => {const original=window.fetch;
                  window.fetch=async(url)=>{
                    if(String(url).includes('product=ETH-USD')) await new Promise(r=>setTimeout(r,400));
                    return original(url);
                  }})()""")
                page.locator('button[data-product="BTC-USD"]').click()
                page.wait_for_function(
                    "document.querySelector('#breadcrumb-product')?.textContent==='BTC-USD'",
                    timeout=12000,
                )
                page.locator('button[data-product="ETH-USD"]').click()
                page.locator('button[data-product="BTC-USD"]').click()
                page.wait_for_timeout(650)
                check(
                    "rapid_product_switch_does_not_cross_contaminate",
                    page.evaluate(
                        (
                            "state.product==='BTC-USD' &&"
                            ' state.trades.every(x=>x.pro'
                            "duct==='BTC-USD') && state.f"
                            'eatures.every(x=>x.instrumen'
                            "t==='BTC-USD')"
                        )
                    ),
                )
                # Race two replay cutoffs, delaying the older snapshot.
                choices = page.locator("#asof option").all()
                early = choices[3].get_attribute("value")
                later = choices[7].get_attribute("value")
                page.evaluate(
                    """(early)=>{const old=window.fetch;
                  window.fetch=async(url)=>{
                    if(String(url).includes('asof='+early))await new Promise(r=>setTimeout(r,300));
                    return old(url);
                  }}""",
                    early,
                )
                page.locator("#asof").select_option(early)
                check(
                    "pending_replay_hides_old_data",
                    page.locator("#verified-views").is_hidden(),
                )
                page.locator("#asof").select_option(later)
                page.wait_for_timeout(550)
                check(
                    "rapid_cutoff_switch_keeps_newest",
                    page.evaluate("(later)=>state.cutoff===Number(later)", later),
                )
                check(
                    "replay_never_shows_future_trade",
                    page.evaluate(
                        "state.trades.every(x=>x.received_at_ms<=state.cutoff)"
                    ),
                )
                page.locator('button[data-view="market"]').click()
                page.screenshot(
                    path=str(output / "v31_standalone_real_archive.png"), full_page=True
                )
                # Deliberately corrupt the apparent lineage of a fresh request.
                page.evaluate("""(() =>{const old=window.fetch;
                  window.fetch=async(url)=>{const r=await old(url);
                    if(String(url).includes('product=ETH-USD') && String(url).includes('kind=features')){
                      const bad=await r.json();bad.projection_sha256='0'.repeat(64);
                      return {ok:true,status:200,json:async()=>bad};
                    }return r;
                  }})()""")
                page.locator('button[data-product="ETH-USD"]').click()
                page.wait_for_function(
                    "document.querySelector('#source-status')?.textContent==='REJECTED'",
                    timeout=12000,
                )
                check(
                    "injected_hash_mismatch_withholds_view",
                    page.locator("#verified-views").is_hidden(),
                )
                check("standalone_no_javascript_exceptions", not errors)
                page.close()
                # Genuine production JS bundle emitted by npm / TypeScript / Vite
                # on GitHub Actions, executed in Chromium (not a source stub).
                page = browser.new_page(viewport={"width": 1460, "height": 990})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.set_content(
                    (
                        '<!doctype html><html><head><'
                        'title>Quant Research</title>'
                        '</head><body><div id="root">'
                        '</div></body></html>'
                    )
                )
                css = list((dist / "assets").glob("*.css"))
                js = list((dist / "assets").glob("*.js"))
                check("production_funnest_assets_exist", len(css) == len(js) == 1)
                page.add_style_tag(content=css[0].read_text())
                equip(page, source_http(port, calls))
                page.add_script_tag(content=js[0].read_text())
                page.wait_for_selector(".uni-chart canvas", timeout=16000)
                page.wait_for_function(
                    (
                        "document.querySelector('.uni"
                        "-chart')?.textContent?.inclu"
                        "des('Coinbase Advanced (publ"
                        "ic spot)')"
                    ),
                    timeout=14000,
                )
                page.wait_for_function(
                    "/O\\s+[\\d,]+\\.\\d+/.test(document.querySelector('.uni-chart')?.textContent||'')",
                    timeout=14000,
                )
                check(
                    "production_real_coinbase_chart_rendered",
                    page.locator(".uni-chart canvas").count() > 0
                    and "BTC-USD" in page.locator(".uni-chart").inner_text(),
                )
                check(
                    "production_funnest_has_numeric_archived_ohlc",
                    page.evaluate(
                        "/O\\s+[\\d,]+\\.\\d+/.test(document.querySelector('.uni-chart')?.textContent||'')"
                    ),
                )
                check(
                    "production_full_history_has_11_completed_bars",
                    "11 completed intervals at cutoff"
                    in page.locator(".uni-panel-caption").first.inner_text(),
                )
                check(
                    "production_legacy_not_co_mingled",
                    page.locator(".legacy-workspace-toggle").count() == 1
                    and page.get_by_text("BTC-USD / ETH-USD · 5-minute OHLCV").count()
                    == 0,
                )
                # Verify user action opens independently labelled legacy dataset.
                page.get_by_role("button", name="Open separate CSV sandbox").click()
                page.get_by_text("BTC-USD / ETH-USD · 5-minute OHLCV").wait_for(
                    timeout=12000
                )
                check(
                    "production_legacy_requires_explicit_open",
                    page.locator(".legacy-source-warning").count() == 1,
                )
                page.get_by_role("button", name="Close separate CSV sandbox").click()
                check(
                    "production_legacy_closed_again",
                    page.get_by_text("BTC-USD / ETH-USD · 5-minute OHLCV").count() == 0,
                )
                # Verify newly selected product is not rendered with old quote.
                page.evaluate("""(() => {const old=window.fetch;
                  window.fetch=async(url)=>{
                    if(String(url).includes('product=ETH-USD')) await new Promise(r=>setTimeout(r,350));
                    return old(url);
                  }})()""")
                page.locator(".uni-switch button").filter(has_text="ETH-USD").click()
                check(
                    "production_stale_BTC_quote_hidden_during_ETH_loading",
                    page.locator(".uni-stats article strong").first.inner_text()
                    == "Unavailable",
                )
                page.wait_for_function(
                    (
                        "document.querySelector('.uni"
                        "-stats article strong')?.tex"
                        "tContent!=='Unavailable'"
                    ),
                    timeout=12000,
                )
                page.wait_for_function(
                    "document.querySelector('.uni-chart')?.textContent?.includes('ETH-USD')",
                    timeout=12000,
                )
                check(
                    "production_ETH_archived_quote_loaded",
                    "ETH-USD" in page.locator(".uni-chart").inner_text(),
                )
                page.locator('select[aria-label="Replay as-of time"]').select_option(
                    index=1
                )
                page.locator(".uni-empty-history").wait_for(timeout=12000)
                check(
                    "production_empty_asof_is_explicit",
                    "No completed 5-minute"
                    in page.locator(".uni-empty-history").inner_text(),
                )
                check(
                    "production_empty_asof_hides_chart",
                    page.locator(".uni-chart canvas").count() == 0,
                )
                page.locator('select[aria-label="Replay as-of time"]').select_option(
                    "latest"
                )
                page.wait_for_function(
                    "/O\\s+[\\d,]+\\.\\d+/.test(document.querySelector('.uni-chart')?.textContent||'')",
                    timeout=14000,
                )
                check(
                    "production_full_history_replay_restored",
                    "11 completed intervals at cutoff"
                    in page.locator(".uni-panel-caption").first.inner_text(),
                )
                check(
                    "production_replay_asof_sent_to_existing_api",
                    any("asof=" in u and "product=ETH-USD" in u for u in calls),
                )
                check("production_no_javascript_exceptions", not errors)
                page.screenshot(
                    path=str(output / "v31_funnest_react_real_archive.png"),
                    full_page=True,
                )
                page.close()
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
    result = {
        "schema": "v31_browser_contract_v1",
        "status": "PASS",
        "checks": checks,
        "check_count": len(checks),
        "browser_navigation": "DISALLOWED_BY_HOST",
        "http": "PYTHON_LOOPBACK_BRIDGE_FROM_CHROMIUM_FETCH_CALLBACK",
        "real_historical_source": True,
        "production_funnest_bundle": True,
        "source_manifest_sha256": (
            '08255d79f0a7f081921f370feca2'
            'e92686cde3dffbb406eadc23e918'
            'd43e73d7'
        ),
        "network_live_source": False,
        "OOS_verified": False,
        "real_orders": 0,
        "js_errors": errors,
        "backend_request_count": len(calls),
        "bundle_sha256": hashlib.sha256(js[0].read_bytes()).hexdigest(),
        "screenshots": [
            "v31_standalone_real_archive.png",
            "v31_funnest_react_real_archive.png",
        ],
    }
    (output / "V31_BROWSER_CONTRACT.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument(
        "--output-dir", type=Path, default=Path("/mnt/data/v31_browser_results")
    )
    args = p.parse_args()
    try:
        result = run(args.output_dir, args.dist)
    except Exception as exc:
        print(
            json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise
    print(json.dumps(result, indent=2, ensure_ascii=False))
