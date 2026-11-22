"""Full real-Coinbase-source Chromium E2E, never mocks market observations.

Requires an environment where Chromium is allowed to navigate to loopback.
The private, SHA-pinned archive stays local; never uploads raw source to CI.
Status is BLOCKED, not PASS, when navigation is administratively disabled.
"""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from v35_replay_service import replay_capture
    from v35_unified_gateway import server_for

    return replay_capture, server_for


replay_capture, server_for = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

DEFAULT_ARCHIVE = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
DEFAULT_PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"


def run(archive: Path, pin: str, browser_executable: str | None = None) -> dict:
    result = {
        "schema": "v35_2_actual_archive_browser_e2e_v1",
        "status": "NOT_EXECUTED",
        "browser_status": "NOT_EXECUTED",
        "market_source_type": "REAL_ARCHIVED_COINBASE",
        "market_data_mocked": False,
        "archive_mode": "ARCHIVE_REPLAY",
        "source_manifest_sha256": pin,
        "verified_products": [],
        "checked_http": [],
        "evidence": [],
        "real_order_count": 0,
    }
    if sha256((archive / "manifest.json").read_bytes()).hexdigest() != pin:
        raise ValueError("EXTERNAL_MANIFEST_PIN_MISMATCH")
    ds = replay_capture(archive, pin)
    assert ds.certificate["admission"] == "VERIFIED_ARCHIVE_ENVELOPE"
    assert ds.certificate["manifest_sha256"] == pin
    result["message_count"] = ds.message_count
    result["final_state_sha256"] = ds.final_state_sha256
    gateway = server_for(ds)
    worker = Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    host = f"http://127.0.0.1:{gateway.server_address[1]}"
    try:
        for endpoint in [
            "/research",
            "/terminal",
            "/research/v35.1",
            "/api/v35/health",
            "/api/v35/snapshot?product=BTC-USD",
            "/api/v35/snapshot?product=ETH-USD",
            "/api/v35/candles?product=BTC-USD",
            "/api/v35/history?product=ETH-USD&limit=2",
        ]:
            with urlopen(host + endpoint, timeout=10) as response:
                if response.status != 200:
                    raise AssertionError(f"HTTP {endpoint}: {response.status}")
                body = response.read()
                if "/api/v35" in endpoint:
                    payload = json.loads(body)
                    if payload["source_manifest_sha256"] != pin:
                        raise AssertionError("HTTP_SOURCE_IDENTITY_MISMATCH")
                result["checked_http"].append(endpoint)
        result["http_status"] = "PASS_REAL_SOURCE"
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            result["browser_status"] = "BLOCKED_PLAYWRIGHT_NOT_INSTALLED"
            result["status"] = "PARTIAL_HTTP_ONLY"
            return result
        try:
            with sync_playwright() as pw:
                kwargs = {"headless": True}
                if browser_executable:
                    kwargs["executable_path"] = browser_executable
                browser = pw.chromium.launch(**kwargs)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.goto(
                        host + "/research/v35.1",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    panel = page.get_by_role(
                        "region", name="v35.1 verified Coinbase archive replay"
                    )
                    panel.wait_for(state="visible", timeout=15000)
                    for product in ("BTC-USD", "ETH-USD"):
                        if product == "ETH-USD":
                            page.locator("button").filter(
                                has_text="ETH-USD"
                            ).first.click()
                        panel.get_by_text("VERIFIED_ARCHIVE_ENVELOPE").wait_for(
                            timeout=10000
                        )
                        panel.get_by_role("button", name="Latest loaded").click()
                        target = ds.rows[product][-1]
                        panel.get_by_text(
                            "v" + str(target["state_version"]), exact=False
                        ).wait_for(timeout=10000)
                        grid = panel.locator('div[style*="grid-template-columns"]')
                        for field in (
                            "best_bid",
                            "best_ask",
                            "spread",
                            "microprice",
                            "ofi_since_connection_base",
                        ):
                            expected = str(target[field])
                            field_card = (
                                grid.locator("div")
                                .filter(has_text=field.replace("_", " ").upper())
                                .first
                            )
                            field_card.locator("strong").wait_for(timeout=10000)
                            actual = field_card.locator("strong").inner_text().strip()
                            if actual != expected:
                                raise AssertionError(
                                    
                                        f"DOM_MARKET_VALUE_MISMATCH {product}"
                                        f" {field}: {actual} != {expected}"
                                    
                                )
                        table = panel.get_by_role(
                            "table", name="Verified replay order book"
                        )
                        first = (
                            table.locator("tbody tr")
                            .first.locator("td")
                            .all_inner_texts()
                        )
                        expected_first = [
                            target["depth"]["bids"][0]["price"],
                            target["depth"]["bids"][0]["quantity"],
                            target["depth"]["asks"][0]["price"],
                            target["depth"]["asks"][0]["quantity"],
                        ]
                        if first != expected_first:
                            raise AssertionError(
                                
                                    f"DOM_BOOK_MISMATCH {product}:"
                                    f" {first} vs {expected_first}"
                                
                            )
                        chart = panel.get_by_label(
                            "Admitted archive trade candle chart"
                        )
                        chart.wait_for(timeout=10000)
                        completed = [
                            b
                            for b in ds.candles[product]
                            if b["available_at"] <= target["receiver_time_ms"]
                        ]
                        if (
                            str(len(completed)) + " candles available"
                            not in chart.inner_text()
                        ):
                            raise AssertionError("FUTURE_CANDLE_AVAILABILITY_MISMATCH")
                        result["evidence"].append(
                            {
                                "product": product,
                                "state_version": target["state_version"],
                                "receiver_time_ms": target["receiver_time_ms"],
                                "source_manifest_sha256": pin,
                                "best_bid": target["best_bid"],
                                "best_ask": target["best_ask"],
                                "spread": target["spread"],
                                "completed_candle_count": len(completed),
                                "first_book_row": first,
                            }
                        )
                        result["verified_products"].append(product)
                    panel.get_by_role("button", name="Check SSE stream").click()
                    panel.get_by_text("SSE VERIFIED", exact=False).wait_for(
                        timeout=10000
                    )
                    if errors:
                        raise AssertionError("UNEXPECTED_JS_ERRORS: " + repr(errors))
                    result["browser_status"] = "PASS_REAL_SOURCE_DOM"
                    result["status"] = "PASS"
                finally:
                    browser.close()
        except Exception as exc:
            if "ERR_BLOCKED_BY_ADMINISTRATOR" in str(exc):
                result["browser_status"] = "BLOCKED_BY_ADMINISTRATOR"
                result["status"] = "PARTIAL_HTTP_ONLY"
                result["blocker"] = str(exc)[:280]
            else:
                result["browser_status"] = "FAILED"
                result["status"] = "FAILED"
                result["error"] = repr(exc)[:1200]
        return result
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=10)
        if worker.is_alive():
            raise RuntimeError("Gateway shutdown did not complete")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    p.add_argument("--manifest-sha256", default=DEFAULT_PIN)
    p.add_argument(
        "--browser-executable", help="Chromium binary on a permitted local host"
    )
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    try:
        result = run(args.archive, args.manifest_sha256, args.browser_executable)
    except Exception as exc:
        result = {"status": "FAILED", "error": repr(exc), "market_data_mocked": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
