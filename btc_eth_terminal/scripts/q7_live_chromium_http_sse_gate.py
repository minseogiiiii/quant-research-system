"""Real public Coinbase read-only Chromium transport acceptance, without fixtures.

This checks real Chromium EventSource and versioned HTTP market-state consistency.
The versioned Q7 archive UI is separately checked for honest source admission.
It deliberately does NOT claim live React/Funnest quote/canvas parity.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

PRODUCTS = ("BTC-USD", "ETH-USD")
TOKEN = re.compile(r"v36:([a-f0-9]{32}):(BTC-USD|ETH-USD):([1-9][0-9]*)\Z")


def read_json(url: str) -> dict:
    with urlopen(url, timeout=5) as r:
        if r.status != 200:
            raise AssertionError("HTTP_NOT_200")
        return json.load(r)


def exact_top(state: dict) -> None:
    if state.get("trading_enabled") is not False or state.get("live_feed") is not True:
        raise AssertionError("UNTRUSTED_STATE_OR_EXECUTION_ENABLED")
    depth = state.get("depth")
    if not isinstance(depth, dict):
        raise AssertionError("MISSING_L2_DEPTH")
    bids, asks = depth.get("bids"), depth.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or len(bids) != 10 or len(asks) != 10:
        raise AssertionError("NOT_EXACT_10x10_DEPTH")
    try:
        bp = Decimal(str(bids[0]["price"]))
        ap = Decimal(str(asks[0]["price"]))
        bq = Decimal(str(bids[0]["quantity"]))
        aq = Decimal(str(asks[0]["quantity"]))
        if (bp <= 0 or ap <= bp or bq <= 0 or aq <= 0 or
                Decimal(str(state["best_bid"])) != bp or
                Decimal(str(state["best_ask"])) != ap or
                Decimal(str(state["spread"])) != ap - bp):
            raise AssertionError("TOP_OF_BOOK_EXACT_DECIMAL_MISMATCH")
    except (KeyError, TypeError, InvalidOperation) as e:
        raise AssertionError("INVALID_DECIMAL_BOOK_STATE") from e


def run(origin: str, executable: str, seconds: int, min_states: int) -> dict:
    from playwright.sync_api import sync_playwright

    initial = read_json(origin + "/api/v36/health")
    if (initial.get("mode") != "LIVE_SOURCE" or initial.get("status") != "HEALTHY"
            or initial.get("live_feed") is not True or initial.get("trading_enabled") is not False
            or set(initial.get("snapshots", [])) != set(PRODUCTS)):
        raise AssertionError("REAL_PUBLIC_LIVE_SOURCE_NOT_ADMITTED")
    session = initial["session_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", session):
        raise AssertionError("INVALID_SOURCE_SESSION")
    page_errors = []
    started = time.monotonic()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.on("pageerror", lambda e: page_errors.append(str(e)[:180]))
            # A real URL navigation is deliberate: no page.route(), local JS
            # server substitution, in-memory fetch overrides or replay fixtures.
            page.goto(origin + "/api/v36/health", wait_until="domcontentloaded", timeout=20000)
            page.evaluate("""() => {
              window.__q7 = {started:Date.now(), traces:{}, errors:[], sources:{}};
              for (const product of ['BTC-USD','ETH-USD']) {
                const trace = window.__q7.traces[product] = [];
                const es = new EventSource('/api/v36/stream?product='+encodeURIComponent(product));
                window.__q7.sources[product] = es;
                for (const type of ['source','snapshot','state','heartbeat','resync_required','stream_rotation']) {
                  es.addEventListener(type, e => {
                    try {
                      const data = JSON.parse(e.data);
                      if (trace.length < 800) trace.push({type, cursor:e.lastEventId||'', data});
                    } catch(err) { window.__q7.errors.push(type+':INVALID_JSON'); }
                  });
                }
                es.addEventListener('error', () => window.__q7.errors.push(product+':EVENTSOURCE_ERROR'));
              }
            }""")
            while time.monotonic() - started < seconds:
                page.wait_for_timeout(1400)
                if page_errors:
                    raise AssertionError("CHROMIUM_PAGE_RUNTIME_ERROR:" + page_errors[0])
                probe = page.evaluate("""() => ({
                  errors:window.__q7.errors.slice(),
                  counts:Object.fromEntries(Object.entries(window.__q7.traces).map(([p,a])=>
                    [p,{source:a.filter(x=>x.type==='source').length,
                        snapshots:a.filter(x=>x.type==='snapshot').length,
                        states:a.filter(x=>x.type==='state').length,
                        resync:a.filter(x=>x.type==='resync_required').length}]))
                })""")
                if probe["errors"]:
                    raise AssertionError("CHROMIUM_EVENTSOURCE_ERROR:" + str(probe["errors"][:2]))
                if (time.monotonic() - started >= 45 and
                    all(probe["counts"][p]["states"] >= min_states for p in PRODUCTS)):
                    break
            traces = page.evaluate("""() => {
               for (const es of Object.values(window.__q7.sources)) es.close();
               return structuredClone(window.__q7.traces);
            }""")
            if page_errors:
                raise AssertionError("CHROMIUM_PAGE_RUNTIME_ERROR:" + page_errors[0])
            summary = {}
            for product in PRODUCTS:
                entries = traces[product]
                handshakes = [e for e in entries if e["type"] == "source"]
                snap = [e for e in entries if e["type"] == "snapshot"]
                states = [e for e in entries if e["type"] == "state"]
                invalid = [e for e in entries if e["type"] in ("resync_required", "stream_rotation")]
                if len(handshakes) != 1 or len(snap) != 1 or len(states) < min_states or invalid:
                    raise AssertionError("CHROMIUM_SSE_SOURCE_OR_EVENT_COUNTS_INVALID:" + product)
                if (handshakes[0]["data"].get("session_id") != session or
                        handshakes[0]["data"].get("product") != product or
                        handshakes[0]["data"].get("trading_enabled") is not False):
                    raise AssertionError("CHROMIUM_SOURCE_HANDSHAKE_INVALID")
                prev = 0
                checked = []
                for e in snap + states:
                    state = e["data"]
                    v = state.get("state_version")
                    token = TOKEN.fullmatch(e["cursor"])
                    if (not isinstance(v, int) or v <= prev or token is None or
                            token.group(1) != session or token.group(2) != product or
                            int(token.group(3)) != v or state.get("session_id") != session or
                            state.get("product") != product):
                        raise AssertionError("CHROMIUM_SSE_CURSOR_ORDER_SOURCE_MISMATCH:" + product)
                    exact_top(state)
                    prev = v
                    checked.append(v)
                # Fetch the same pinned versions over actual HTTP, after the
                # browser received them from the independent EventSource path.
                for e in (snap + states)[-min(3, len(snap + states)):]:
                    v = e["data"]["state_version"]
                    request = "/api/v36/state?" + urlencode({"product":product,"session":session,"version":v})
                    live = read_json(origin + request)["state"]
                    exact_top(live)
                    for field in ("best_bid", "best_ask", "spread", "microprice", "best_imbalance", "ofi_since_connection_base"):
                        if Decimal(str(live[field])) != Decimal(str(e["data"][field])):
                            raise AssertionError("HTTP_SSE_NUMERIC_MISMATCH:" + product + ":" + field)
                    if live["depth"] != e["data"]["depth"]:
                        raise AssertionError("HTTP_SSE_L2_DEPTH_MISMATCH:" + product)
                summary[product] = {"source_handshakes":len(handshakes),"snapshots":len(snap),
                                    "states":len(states),"distinct_versions":len(checked),
                                    "pinned_http_versions_compared":min(3,len(snap + states)),
                                    "unexpected_resync":len(invalid)}
            current = read_json(origin + "/api/v36/health")
            if (current.get("session_id") != session or current.get("status") != "HEALTHY"
                    or current.get("trading_enabled") is not False):
                raise AssertionError("SOURCE_SESSION_ROTATION_OR_UNHEALTHY")
            # Reuse a genuine Chromium browser for the actual pinned Q7 app.
            # Without archival fixtures the correct state is BLOCKED, not
            # synthetic 'research alpha'. This is NOT live React quote parity.
            response = page.goto(origin + "/research/v37", wait_until="domcontentloaded", timeout=20000)
            if response is None or response.status != 200:
                raise AssertionError("Q7_VERSIONED_BROWSER_ASSETS_NOT_DELIVERED")
            page.locator(".pro-topbar").wait_for(timeout=18000)
            page.locator(".pro-empty").wait_for(timeout=15000)
            if "NO LIVE FEED" not in page.locator(".pro-system").inner_text().upper():
                raise AssertionError("ARCHIVED_Q7_UI_IMPERSONATES_LIVE")
            if page_errors:
                raise AssertionError("CHROMIUM_Q7_REACT_ERROR:" + page_errors[0])
        finally:
            browser.close()
    return {"status":"PASS_REAL_CHROMIUM_HTTP_EVENTSOURCE_SESSION_VERSION_DEPTH",
            "schema":"q7_chromium_live_transport_v1",
            "mode":"REAL_PUBLIC_COINBASE_LIVE_NO_SYNTHETIC_TRANSPORT",
            "source_session_id":session,"elapsed_seconds":round(time.monotonic()-started,2),
            "products":summary,"q7_archive_ui":"REAL_CHROMIUM_HTTP_FAIL_CLOSED_NO_ARCHIVE_FIXTURE",
            "live_react_quote_dom_parity":"NOT_VERIFIED",
            "canvas_pixel_parity":"NOT_VERIFIED",
            "execution_enabled":False,"persistent_24h":"NOT_VERIFIED"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--origin", default="http://127.0.0.1:8766")
    p.add_argument("--browser-executable", required=True)
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--min-states", type=int, default=15)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    if not 60 <= args.seconds <= 240 or not 5 <= args.min_states <= 200:
        p.error("seconds must be 60..240 and min-states must be 5..200")
    try:
        result = run(args.origin,args.browser_executable,args.seconds,args.min_states)
        code = 0
    except Exception as e:
        result = {"schema":"q7_chromium_live_transport_v1","status":"FAIL",
                  "reason":type(e).__name__ + ":" + str(e)[:220],
                  "synthetic_transport":False,"execution_enabled":False}
        code = 2
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,sort_keys=True,indent=2)+"\n")
    output = {k:v for k,v in result.items() if k!="source_session_id"}
    print("Q7_CHROMIUM_LIVE_GATE " + json.dumps(output,sort_keys=True),flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())