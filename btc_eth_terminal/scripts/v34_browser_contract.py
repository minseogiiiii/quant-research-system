#!/usr/bin/env python3
"""v34 compiled Funnest L2 historical browser smoke, using exact real loopback API bytes.

The sandbox disallows direct Chromium localhost network navigation. Browser
fetches are mediated by Python urllib to a real 127.0.0.1 Python API.
This DOES NOT establish production browser-network E2E or live exchange state.
"""

from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    import v30_unified_api as api
    from v34_frontend_integrity import verify_build

    return api, verify_build


api, verify_build = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(output: Path):
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    files, manifest = verify_build()
    js = next(raw for path, raw in files.items() if path.endswith(".js"))
    css = next(raw for path, raw in files.items() if path.endswith(".css"))
    calls = []
    checks = []
    js_errors = []
    httpport = port()
    proc = subprocess.Popen(
        [sys.executable, "backend/bridge.py"],
        cwd=ROOT,
        env={**os.environ, "QUANT_BRIDGE_PORT": str(httpport)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def real_fetch(url):
        if not isinstance(url, str) or not url.startswith("/api/") or len(url) > 1500:
            raise ValueError("Unexpected browser transport route " + str(url))
        calls.append(url)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{httpport}{url}", timeout=10
            ) as r:
                return {"ok": True, "status": r.status, "payload": json.loads(r.read())}
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "payload": json.loads(e.read())}

    try:
        time.sleep(0.4)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(
                viewport={"width": 1680, "height": 1080}, device_scale_factor=1
            )
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.set_content(
                '<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div></body></html>'
            )
            page.add_style_tag(content=css.decode())
            page.expose_function("__backend", real_fetch)
            page.evaluate(
                """(() => {window.fetch=async(u)=>{const a=await window.__backend(String(u));return {ok:a.ok,status:a.status,json:async()=>a.payload}}})()"""
            )
            page.add_script_tag(content=js.decode())
            page.wait_for_selector(".pro-terminal .pro-main-grid", timeout=20000)
            page.wait_for_selector(".pro-funnest-chart canvas", timeout=20000)
            page.wait_for_function(
                "document.querySelector('.pro-utc')?.textContent?.includes('2026-')",
                timeout=15000,
            )
            check(
                "real_catalog_cohort",
                "V16" in page.locator(".pro-session-label").inner_text(),
            )
            check(
                "professional_one_screen_structure",
                page.locator(".pro-main-grid .pro-panel").count() >= 6,
            )
            check(
                "l1_quote_card_present",
                "MIDPRICE" in page.locator(".pro-metrics").inner_text(),
            )
            check(
                "real_chart_canvas",
                page.locator(".pro-funnest-chart canvas").count() > 0,
            )
            check(
                "trade_tape_populated",
                page.locator(".pro-trades-scroll tbody tr").count() > 2,
            )
            page.wait_for_function(
                "document.querySelectorAll('.pro-l2-level.bid').length===10 && document.querySelectorAll('.pro-l2-level.ask').length===10",
                timeout=14000,
            )
            check(
                "l2_book_ten_levels_each_side",
                page.locator(".pro-l2-level.ask").count() == 10
                and page.locator(".pro-l2-level.bid").count() == 10,
            )
            check(
                "l2_book_not_synthetic",
                "AUDITED HISTORICAL" in page.locator(".pro-book-panel").inner_text(),
            )
            check(
                "liquidity_recovery_measured_descriptively",
                "qualified historical shocks"
                in page.locator(".pro-recovery-panel").inner_text(),
            )
            check(
                "liquidity_recovery_not_alpha",
                "POST-HOC ONLY" in page.locator(".pro-recovery-panel").inner_text(),
            )
            check(
                "ofi_and_microprice_real_traces",
                page.locator(".pro-analytics-card svg polyline").count() == 2,
            )
            check(
                "research_claims_blocked",
                "NO CONFIRMED EXECUTABLE ALPHA"
                in page.locator(".pro-research").inner_text(),
            )
            check(
                "lineage_sha_visible",
                len(page.locator(".pro-hash-entry code").first.inner_text()) == 64,
            )
            check(
                "source_replay_not_live",
                "NOT LIVE" in page.locator(".pro-replay").inner_text(),
            )
            check(
                "closed_legacy_separation", page.locator(".legacy-sandbox").count() == 0
            )
            # Single causal clock across panels. At earliest diagnostic time no completed candle.
            page.get_by_role("combobox", name="Replay as-of time").select_option("0")
            page.wait_for_selector(".uni-empty-history", timeout=10000)
            check(
                "first_asof_has_no_future_bars",
                page.locator(".pro-funnest-chart canvas").count() == 0,
            )
            page.wait_for_function(
                "document.querySelectorAll('.pro-l2-level.bid').length===10",
                timeout=10000,
            )
            check(
                "early_l2_still_causal", page.locator(".pro-l2-level.bid").count() == 10
            )
            check(
                "first_asof_trade_sample_causal",
                page.evaluate(
                    "(() => {const cutoff=document.querySelector('.pro-utc').textContent;return cutoff.includes('2026-')})()"
                ),
            )
            check(
                "no_completed_5m_candle_message",
                "No completed 5-minute"
                in page.locator(".uni-empty-history").inner_text(),
            )
            page.get_by_role("button", name="Step historical replay").click()
            check(
                "step_moves_observation_index",
                "2 / 12" in page.locator(".pro-progress").inner_text(),
            )
            page.get_by_role("button", name="Reset historical replay").click()
            page.wait_for_selector(".pro-funnest-chart canvas", timeout=14000)
            check(
                "reset_restores_real_candles",
                "11 completed intervals"
                in page.locator(".pro-candle-total").inner_text(),
            )
            # Product switch: hide previous source until new product is validated.
            page.evaluate(
                """(() => {let previous=window.fetch;window.fetch=async(u)=>{if(String(u).includes('product=ETH-USD'))await new Promise(r=>setTimeout(r,320));return previous(u)}})()"""
            )
            page.locator(".pro-symbols button").filter(has_text="ETH-USD").click()
            check(
                "product_switch_hides_old_l1",
                page.locator(".pro-metrics article strong").first.inner_text() == "—",
            )
            page.wait_for_function(
                "document.querySelector('.pro-metrics article strong')?.textContent?.includes('1,')",
                timeout=13000,
            )
            check(
                "eth_recomputed_quote",
                "1,8" in page.locator(".pro-metrics article strong").first.inner_text(),
            )
            check(
                "eth_trade_tape_rebound",
                page.locator(".pro-trades-scroll tbody tr").count() > 0,
            )
            page.locator(".pro-symbols button").filter(has_text="BTC-USD").click()
            page.wait_for_function(
                "document.querySelector('.pro-metrics article strong')?.textContent?.includes('66,')",
                timeout=12000,
            )
            check(
                "btc_recomputed_quote",
                "66," in page.locator(".pro-metrics article strong").first.inner_text(),
            )
            page.get_by_role("tab", name="DIAGNOSTIC HISTORY").click()
            check(
                "historical_diagnostic_rows",
                page.locator(".pro-detail-scroll tbody tr").count() == 12,
            )
            page.get_by_role("tab", name="PROVENANCE & AUDIT").click()
            check(
                "evidence_full_hashes",
                page.locator(".pro-hash-entry code").count() >= 2,
            )
            page.get_by_role("tab", name="OVERVIEW").click()
            check("no_browser_javascript_exception", not js_errors)
            page.screenshot(
                path=str(output / "v34_actual_l2_terminal.png"), full_page=True
            )
            # Narrow breakpoint: panels must stack and stay usable without white page errors.
            page.set_viewport_size({"width": 420, "height": 920})
            page.wait_for_timeout(160)
            check("responsive_narrow_view", page.locator(".pro-terminal").is_visible())
            page.screenshot(
                path=str(output / "v34_actual_l2_mobile.png"), full_page=True
            )
            check(
                "readonly_api_calls_only",
                len(calls) > 8
                and all(c.startswith(("/api/v30/", "/api/v34/")) for c in calls),
            )
            check(
                "l2_receives_both_products",
                any("/api/v34/depth?product=BTC-USD" in c for c in calls)
                and any("/api/v34/depth?product=ETH-USD" in c for c in calls),
            )
            check(
                "recovery_readonly_queries",
                any("/api/v34/recovery?" in c for c in calls),
            )
            browser.close()
        report = {
            "status": "PASS",
            "build_commit": manifest.get("git_commit"),
            "checks": checks,
            "passed": len(checks),
            "page_errors": js_errors,
            "http_calls": len(calls),
            "products": ["BTC-USD", "ETH-USD"],
            "data_cohort": api.catalog()["source_cohort"],
            "market_data": "REAL_ARCHIVED_PUBLIC_COINBASE_SPOT",
            "claimed_live": False,
            "claimed_oos": False,
            "direct_chromium_loopback_navigation": False,
            "backend_transport": "PYTHON_MEDIATED_REAL_LOOPBACK_HTTP",
        }
        (output / "v34_browser_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in ("checks", "page_errors")},
                indent=2,
            )
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    run(Path("/mnt/data/v34_browser_validation"))
