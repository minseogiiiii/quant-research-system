#!/usr/bin/env python3
"""Q6 Chromium DOM verification of freshly built Q6 React bundle with archived-state fixtures.

Browser network is prohibited in this runtime. This harness does NOT test HTTP/SSE
transport or claim genuine LIVE. It changes only an IN-MEMORY copy of the built
entrypoint route conditional to load /research/v36 from an about:blank document.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(
    os.environ.get("Q6_BROWSER_OUTPUT_DIR", str(ROOT / "verification/v36_3_r5_q6"))
).resolve()
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))


def _q14_load_after_path_setup_0():
    from v36_live_engine import LiveMarketEngine
    from v36_play_verified_archive import admitted_verified_chunks

    return LiveMarketEngine, admitted_verified_chunks


LiveMarketEngine, admitted_verified_chunks = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

capture = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
pin = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
src = admitted_verified_chunks(capture, pin)
certificate, _ = next(src)
engine = LiveMarketEngine()
messages = 0
for _, b in src:
    for row in b.splitlines(keepends=True):
        engine.consume(row)
        messages += 1
assert messages == certificate["messages"]
health = engine.health()
products = ["BTC-USD", "ETH-USD"]
snapshots = {p: engine.snapshot(p) for p in products}
candles = {p: engine.candles_page(p) for p in products}
assert health["status"] == "HEALTHY" and health["mode"] == "ARCHIVE_SIMULATION"
for p in products:
    assert snapshots[p]["state"] and snapshots[p]["state"]["product"] == p

bundle = ROOT / "frontend/dist_v36/assets/index-BpiBDnMe.js"
css = ROOT / "frontend/dist_v36/assets/index-Ckn1nBC7.css"
original = bundle.read_bytes()
route = 'window.location.pathname==="/research/v36"'
assert original.count(route.encode()) == 1, "Test-only pathname location not unique"
# This local test wrapper is explicitly not a production artifact.
js = original.decode().replace(route, "true", 1).replace("</script", "<\\/script")
payload = {"health": health, "snapshots": snapshots, "candles": candles}
prelude = r"""<script>
window.__responses=PAYLOAD_LITERAL;
window.__seenFetch=[];
window.fetch=async function(input,options){
 const raw=typeof input==='string'?input:input.url;
 const u=new URL(raw,'http://test.invalid');
 window.__seenFetch.push(u.pathname+u.search);
 if(u.pathname==='/api/v36/health'&&window.__hangHealth){
  return await new Promise((_resolve,reject)=>{
   const signal=options?.signal;
   if(signal?.aborted)return reject(signal.reason ?? new DOMException('aborted','AbortError'));
   signal?.addEventListener('abort',()=>reject(signal.reason ?? new DOMException('aborted','AbortError')),{once:true});
  });
 }
 let body=null;
 if(u.pathname==='/api/v36/health') body=window.__responses.health;
 if(u.pathname==='/api/v36/snapshot') body=window.__responses.snapshots[u.searchParams.get('product')];
 if(u.pathname==='/api/v36/candles') body=window.__responses.candles[u.searchParams.get('product')];
 if(body==null)return new Response(JSON.stringify({error:'NOT_INTERCEPTED'}),{status:404,headers:{'Content-Type':'application/json'}});
 return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}});
};
window.__mockEventSources=[];
window.EventSource=class OfflineEventSource{
 constructor(url){this.url=url;this.listeners={};this.closed=false;window.__mockEventSources.push(this)}
 addEventListener(name,cb){(this.listeners[name]??=[]).push(cb)}
 close(){this.closed=true}
 fire(name,data){for(const f of this.listeners[name]??[])f({data:JSON.stringify(data)})}
};
</script>""".replace(
    "PAYLOAD_LITERAL",
    json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/"),
)
pagehtml = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    + css.read_text()
    + "</style>"
    + prelude
    + '</head><body><div id="root"></div><script type="module">'
    + js
    + "</script></body></html>"
)
report = {
    "schema": "q6_fresh_bundle_offline_dom_parity_v1",
    "classification": "VERIFIED_BROWSER_ARCHIVE_SIMULATED_TRANSPORT",
    "source": "genuine_coinbase_archive",
    "mode": "ARCHIVE_SIMULATION",
    "manifest_pin": pin,
    "source_messages": messages,
    "frozen_bundle_sha256": hashlib.sha256(original).hexdigest(),
    "test_only_bundle_route_override": True,
    "browser_http_network": "BLOCKED_BY_ADMINISTRATOR",
    "actual_sse": "NOT_TESTED",
    "clean_npm_build": "PASS_FROM_LOCKFILE_RESOLVED_DEPENDENCY_ARTIFACT",
    "products": {},
    "fault_injection": {},
    "page_errors": [],
}
with sync_playwright() as pw:
    browser = pw.chromium.launch(
        executable_path="/usr/bin/chromium",
        headless=True,
        args=["--disable-dev-shm-usage", "--no-sandbox"],
    )
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
        page.set_content(pagehtml, wait_until="domcontentloaded", timeout=15000)
        page.get_by_role("heading", name="BTC/ETH Live Market Research").wait_for(
            timeout=15000
        )
        for p in products:
            page.get_by_role("button", name=p, exact=True).click()
            s = snapshots[p]["state"]
            page.get_by_role("status").filter(
                has_text=f"state version {s['state_version']}"
            ).wait_for(timeout=15000)
            assert page.get_by_text("ARCHIVE SIMULATION · NOT LIVE").count() > 0
            observed = page.evaluate("""() => {
     const t=document.querySelector('[aria-label="Validated top of book"]');
     let cards={}; for(const e of t.querySelectorAll(':scope > div')){
       const label=e.querySelector('small')?.textContent?.trim();
       const value=e.querySelector('strong')?.textContent?.trim();
       if(label)cards[label]=value;
     }
     const depth=document.querySelector('[aria-label="Live order book 10 levels"]');
     return {cards,rows:Array.from(depth.querySelectorAll('tbody tr'),tr=>Array.from(tr.querySelectorAll('td'),td=>td.textContent?.trim())),
       product:document.querySelector('button[aria-pressed="true"]')?.textContent?.trim(),
       datafeed:document.querySelector('[data-testid="funnest-actual-getbars"]')?.getAttribute('data-bars'),
       canvas_count:document.querySelectorAll('canvas').length,
       sse_created:window.__mockEventSources.length};
   }""")
            assert observed["product"] == p, observed
            for k in (
                "best_bid",
                "best_ask",
                "spread",
                "microprice",
                "best_imbalance",
                "ofi_since_connection_base",
            ):
                display = observed["cards"].get(k.upper().replace("_", " "))
                assert display == s[k], f"{p}: {k} {display} != {s[k]}"
            expected = [
                [
                    s["depth"]["bids"][i]["price"],
                    s["depth"]["bids"][i]["quantity"],
                    s["depth"]["asks"][i]["price"],
                    s["depth"]["asks"][i]["quantity"],
                ]
                for i in range(10)
            ]
            assert observed["rows"] == expected, f"order book mismatch {p}"
            report["products"][p] = {
                "state_version": s["state_version"],
                "best_bid": s["best_bid"],
                "best_ask": s["best_ask"],
                "spread": s["spread"],
                "microprice": s["microprice"],
                "ofi": s["ofi_since_connection_base"],
                "depth_10_rows_matched": 10,
                "completed_candles": len(candles[p]["rows"]),
                "canvases": observed["canvas_count"],
                "browser_datafeed_bars": len(json.loads(observed["datafeed"] or "[]")),
                "active_test_eventsource_instances": observed["sse_created"],
            }
            if p == "ETH-USD":
                page.screenshot(
                    path=str(OUT / "terminal_archive_parity.png"), full_page=True
                )
        count = page.evaluate("""() => {
    const subs=window.__mockEventSources.filter(s=>!s.closed);
    if(subs.length!==1)return -subs.length;
    subs[0].fire('resync_required',{reason:'SYNTHETIC_TEST_GAP'});
    return subs.length;
  }""")
        assert count == 1, f"Expected one active test source, got {count}"
        page.get_by_role("heading", name="No currently trusted market quote").wait_for(
            timeout=5000
        )
        assert page.get_by_role("region", name="Validated top of book").count() == 0
        assert page.get_by_role("alert").get_by_text("SOURCE_GAP").count() > 0
        report["fault_injection"] = {
            "synthetic_resync_required": "PASS_QUOTES_HIDDEN_AND_ALERTED"
        }
        report["api_fetches"] = page.evaluate("window.__seenFetch")[:30]
        assert not report["page_errors"], report["page_errors"]
        # Freshly rebuilt Q6 must reject a tampered but otherwise plausible quote.
        old = (
            '"best_imbalance":"' + snapshots["BTC-USD"]["state"]["best_imbalance"] + '"'
        )
        assert old in pagehtml
        badhtml = pagehtml.replace(old, '"best_imbalance":"NaN"', 1)
        bad_page = browser.new_page(viewport={"width": 1200, "height": 900})
        bad_page.set_content(badhtml, wait_until="domcontentloaded", timeout=15000)
        bad_page.get_by_role(
            "heading", name="No currently trusted market quote"
        ).wait_for(timeout=10000)
        assert bad_page.get_by_role("region", name="Validated top of book").count() == 0
        report["negative_probes"] = {"best_imbalance_NaN_rejected": "PASS"}
        bad_page.close()
        # Synthetic hung Health endpoint: render must fail closed without a new
        # HTTP response.
        hanging = browser.new_page(viewport={"width": 1200, "height": 900})
        hanging.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
        hanging.set_content(pagehtml, wait_until="domcontentloaded", timeout=15000)
        hanging.get_by_role("region", name="Validated top of book").wait_for(
            timeout=10000
        )
        hanging.evaluate("window.__hangHealth=true")
        hanging.get_by_role(
            "heading", name="No currently trusted market quote"
        ).wait_for(timeout=16000)
        assert hanging.get_by_role("region", name="Validated top of book").count() == 0
        report["fault_injection"]["hung_health_probe"] = {
            "trusted_quote_hidden": "PASS",
            "alert": hanging.get_by_role("alert").inner_text(timeout=2000),
        }
        hanging.close()
        assert not report["page_errors"], report["page_errors"]
        report["status"] = (
            "PASS_FRESH_BUNDLE_ARCHIVE_SIMULATED_TRANSPORT_WITH_HTTP_BLOCKED"
        )
    finally:
        browser.close()
(OUT / "q6_fresh_bundle_chromium_report.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n"
)
print(
    json.dumps(
        {
            "status": report["status"],
            "products": report["products"],
            "fault_injection": report["fault_injection"],
            "page_errors": report["page_errors"],
        },
        sort_keys=True,
    )
)
