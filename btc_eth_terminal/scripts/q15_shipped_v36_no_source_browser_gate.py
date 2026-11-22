"""Q15: unchanged v36 shipped browser over real loopback HTTP, no admitted source.

Tests the genuine SHA-pinned HTML/JS/CSS served by the original gateway, not
a rebuilt preview, route override, mocked fetch, or mocked EventSource.
This tests fail-closed UI only, NOT LIVE, real archival replay, or 24h uptime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from v36_frontend_integrity import v36_asset
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
MANIFEST = FRONTEND / "v36_dist_integrity.json"


def validate_pins() -> dict[str, str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("release") != "v36_engineering_candidate_read_only":
        raise ValueError("Q15_UNEXPECTED_SHIPPED_RELEASE")
    if manifest.get("trading_enabled") is not False:
        raise ValueError("Q15_SHIPPED_TRADING_CAPABILITY_FORBIDDEN")
    expected = manifest["files"]
    if len(expected) != 3:
        raise ValueError("Q15_SHIPPED_FILE_COUNT_CHANGED")
    root = FRONTEND / "dist_v36"
    found = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }
    if set(found) != set(expected):
        raise ValueError("Q15_SHIPPED_FILESET_CHANGED")
    hashes: dict[str, str] = {}
    for name, entry in expected.items():
        path = found[name]
        if path.is_symlink():
            raise ValueError("Q15_SHIPPED_SYMLINK")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != entry["bytes"] or digest != entry["sha256"]:
            raise ValueError("Q15_SHIPPED_BUNDLE_HASH_MISMATCH")
        hashes[name] = digest
    for route, name in (
        ("/research/v36", "index.html"),
        ("/assets/index-BpiBDnMe.js", "assets/index-BpiBDnMe.js"),
        ("/assets/index-Ckn1nBC7.css", "assets/index-Ckn1nBC7.css"),
    ):
        data, _content_type = v36_asset(route)
        if hashlib.sha256(data).hexdigest() != hashes[name]:
            raise ValueError("Q15_SHIPPED_SERVER_ASSET_MISMATCH")
    return hashes


def http_json(origin: str, endpoint: str, expected_status: int) -> dict:
    try:
        with urlopen(origin + endpoint, timeout=4) as response:
            status = response.status
            payload = json.load(response)
    except HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read())
    if status != expected_status:
        raise AssertionError(f"Q15_UNEXPECTED_HTTP_STATUS:{endpoint}:{status}")
    return payload


def audit(browser_executable: str, screenshot: Path | None) -> dict:
    from playwright.sync_api import sync_playwright

    hashes = validate_pins()
    engine = LiveMarketEngine()
    server = server_for_live(engine, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        health = http_json(origin, "/api/v36/health", 200)
        if not (
            health["status"] == "CONNECTING"
            and health["live_feed"] is False
            and health["trading_enabled"] is False
            and health["execution_authorized"] is False
            and health["snapshots"] == []
        ):
            raise AssertionError("Q15_UNADMITTED_SOURCE_NOT_FAIL_CLOSED")
        for product in ("BTC-USD", "ETH-USD"):
            response = http_json(
                origin, f"/api/v36/snapshot?product={product}", 503
            )
            if response.get("state") is not None:
                raise AssertionError("Q15_QUOTE_VISIBLE_WITHOUT_SOURCE")
        page_errors: list[str] = []
        seen_requests: list[str] = []
        failed_requests: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=browser_executable,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "request",
                    lambda request: seen_requests.append(
                        request.url.split("?")[0]
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request: failed_requests.append(request.url),
                )
                response = page.goto(
                    origin + "/research/v36",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                if response is None or response.status != 200:
                    raise AssertionError("Q15_SHIPPED_PAGE_NOT_SERVED")
                page.get_by_role(
                    "heading", name="BTC/ETH Live Market Research"
                ).wait_for(timeout=15000)
                for product in ("BTC-USD", "ETH-USD"):
                    page.get_by_role(
                        "button", name=product, exact=True
                    ).click()
                    page.get_by_role(
                        "heading", name="No currently trusted market quote"
                    ).wait_for(timeout=15000)
                    if page.get_by_role(
                        "region", name="Validated top of book"
                    ).count():
                        raise AssertionError("Q15_UNTRUSTED_QUOTE_RENDERED")
                    selected = page.get_by_role(
                        "button", name=product, exact=True
                    ).get_attribute("aria-pressed")
                    if selected != "true":
                        raise AssertionError("Q15_INSTRUMENT_SWITCH_BROKEN")
                body = page.locator("body").inner_text().lower()
                if "orders disabled" not in body:
                    raise AssertionError("Q15_NO_DISABLED_ORDERS_NOTICE")
                if page.locator("main.v36-live-terminal").count() != 1:
                    raise AssertionError("Q15_SHIPPED_COMPONENT_NOT_MOUNTED")
                if screenshot is not None:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot), full_page=False)
                if page_errors:
                    raise AssertionError(
                        "Q15_JS_RUNTIME_ERROR:" + page_errors[0][:180]
                    )
                if not any(
                    "/assets/index-BpiBDnMe.js" in path
                    for path in seen_requests
                ):
                    raise AssertionError("Q15_SHIPPED_JS_NOT_LOADED")
                if not any(
                    "/api/v36/health" in path for path in seen_requests
                ):
                    raise AssertionError("Q15_REAL_HEALTH_HTTP_NOT_FETCHED")
                if failed_requests:
                    raise AssertionError("Q15_FAILED_BROWSER_REQUEST")
                return {
                    "schema": "q15_shipped_v36_no_source_http_dom_v1",
                    "verdict": "PASS_V36_SHIPPED_DOM_FAIL_CLOSED",
                    "browser_transport": "REAL_LOOPBACK_HTTP",
                    "shipped_js_rewritten": False,
                    "mocked_fetch_or_eventsource": False,
                    "market_source": "NONE_NOT_ADMITTED",
                    "private_historical_archive_used": False,
                    "live_source_tested": False,
                    "twenty_four_hour_operation_tested": False,
                    "shipped_v36_sha256": hashes,
                    "products_with_no_admitted_quote": ["BTC-USD", "ETH-USD"],
                    "page_errors": len(page_errors),
                    "trading_enabled": False,
                    "release_approved": False,
                }
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    report = audit(args.browser_executable, args.screenshot)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("PASS_Q15_SHIPPED_V36_NO_SOURCE_REAL_HTTP")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
