"""F1-C: actual loopback gateway / Chromium parity, not trading certification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger

PRODUCTS = ("BTC-USD", "ETH-USD")
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def need(ok, code):
    if not ok:
        raise ValueError(code)


def key(body):
    return (
        body["gateway_run"],
        body["generation"],
        body["source_session"],
        *(body["states"][p]["state_version"] for p in PRODUCTS),
    )


def projection(body):
    return {
        "run": body["gateway_run"],
        "generation": body["generation"],
        "session": body["source_session"],
        "source": body["active_source"],
        "rows": {
            p: {
                "version": row["state_version"],
                "sequence": row["source_sequence"],
                "hash": row["source_raw_sha256"],
                "receiver": row["receiver_time_ms"],
                "bid": row["best_bid"],
                "ask": row["best_ask"],
            }
            for p, row in body["states"].items()
        },
    }


DOM = """() => {
 const status=document.querySelector('[data-testid=market-status]')?.textContent;
 const route=document.querySelector('[data-testid=route]');
 const articles=[...document.querySelectorAll('article[data-product]')];
 if(!route)return {status,rows:articles.length};
 const rows=Object.fromEntries(articles.map(a=>[a.dataset.product,{
 version:Number(a.dataset.version),sequence:Number(a.dataset.sequence),
 hash:a.dataset.hash,receiver:Number(a.dataset.receiverMs),
 bid:a.querySelector('[data-testid=bid]').textContent,
 ask:a.querySelector('[data-testid=ask]').textContent}]));
 return {status,run:route.dataset.run,generation:Number(route.dataset.generation),
 session:route.dataset.session,source:route.dataset.source,rows};
}"""
WATCH = """() => {
 window.__f1Transitions=[];
 const remember=()=>{
   const status=document.querySelector('[data-testid=market-status]')?.textContent;
   const route=document.querySelector('[data-testid=route]');
   if(!status)return;
   const v={status,session:route?.dataset.session??null,
     rows:document.querySelectorAll('article[data-product]').length};
   const old=window.__f1Transitions.at(-1);
   if(JSON.stringify(v)!==JSON.stringify(old)){
     if(window.__f1Transitions.length>=256)throw Error('TRANSITION_LIMIT');
     window.__f1Transitions.push(v);
   }
 };
 new MutationObserver(remember).observe(document.documentElement,
   {childList:true,subtree:true,attributes:true,characterData:true});
 remember();
}"""


def run(root, synthetic):
    root.mkdir(mode=0o700)
    sources = [
        ManagedSource(n, root / n, 180, synthetic, max_restarts=1)
        for n in ("primary", "secondary")
    ]
    route = ResilientRoute(*sources)
    ledger = TransitionLedger(root / "route.jsonl")
    publisher = Publisher(route, ledger)
    server = BoundedServer(publisher, port=8789)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    vite = None
    publisher.start()
    thread.start()
    result = {
        "mode": "SYNTHETIC_REJECTION" if synthetic else "REAL_PUBLIC",
        "status": "FAIL",
        "real_orders_enabled": False,
        "zero_gap_certified": False,
        "dom_matches": 0,
    }
    try:
        vite = subprocess.Popen(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "--config",
                "vite.fincept.config.ts",
            ],
            cwd=FRONTEND,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        end = time.monotonic() + 20
        while time.monotonic() < end:
            need(vite.poll() is None, "F1_VITE_EXIT")
            try:
                with urlopen("http://127.0.0.1:5174/resilient.html", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            raise ValueError("F1_VITE_TIMEOUT")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            errors = []
            frames = {}
            stream_frames = set()

            def receive(body, streamed=False):
                if not isinstance(body, dict) or body.get("states") is None:
                    return
                if body.get("mode") != "LIVE_READ_ONLY_CANDIDATE":
                    return
                k = key(body)
                frames[k] = projection(body)
                if streamed:
                    stream_frames.add(k)
                need(len(frames) <= 4096, "F1_FRAME_LIMIT")

            def response(r):
                if r.url.endswith("/q292/snapshot") and r.status == 200:
                    receive(r.json())

            def event(message):
                if message.get("eventName") == "snapshot":
                    receive(json.loads(message["data"]), streamed=True)

            page.on("pageerror", lambda _: errors.append("PAGE_ERROR"))
            page.on("response", response)
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.on("Network.eventSourceMessageReceived", event)
            page.goto(
                "http://127.0.0.1:5174/resilient.html", wait_until="domcontentloaded"
            )
            page.evaluate(WATCH)
            if synthetic:
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    body = publisher.read()
                    if body["states"] is not None:
                        break
                    page.wait_for_timeout(100)
                else:
                    raise ValueError("F1_SYNTHETIC_SOURCE_NOT_READY")
                for _ in range(30):
                    dom = page.evaluate(DOM)
                    need(
                        dom["status"] == "BLOCKED" and dom["rows"] == 0,
                        "F1_SYNTHETIC_DISPLAYED",
                    )
                    page.wait_for_timeout(100)
                result["synthetic_rejected"] = True
            else:
                matched = set()

                def observe(timeout, count, excluded=None):
                    end = time.monotonic() + timeout
                    local = set()
                    while time.monotonic() < end:
                        need(not errors and not publisher.failure, "F1_RUNTIME")
                        dom = page.evaluate(DOM)
                        if dom["status"] == "READY":
                            k = (
                                dom["run"],
                                dom["generation"],
                                dom["session"],
                                *(dom["rows"][p]["version"] for p in PRODUCTS),
                            )
                            actual = {k1: v for k1, v in dom.items() if k1 != "status"}
                            if k in frames:
                                need(actual == frames[k], "F1_DOM_MISMATCH")
                                if k in stream_frames and dom["session"] != excluded:
                                    matched.add(k)
                                    local.add(k)
                                    if len(local) >= count:
                                        return dom
                        else:
                            need(dom["rows"] == 0, "F1_BLOCKED_QUOTE_LEAK")
                        page.wait_for_timeout(100)
                    raise ValueError("F1_PARITY_TIMEOUT")

                first = observe(35, 8)
                end = time.monotonic() + 12
                while time.monotonic() < end:
                    body = publisher.read()
                    if (
                        body["source_session"] == first["session"]
                        and set(body.get("checkpoint_comparison", {}).values())
                        <= {"MATCH", "NOT_COMPARABLE"}
                        and len(body.get("checkpoint_comparison", {})) == 2
                    ):
                        break
                    page.wait_for_timeout(100)
                else:
                    raise ValueError("F1_STANDBY_NOT_READY")
                target = next(s for s in sources if s.name == first["source"])
                need(target.worker is not None, "F1_TARGET_MISSING")
                t0 = time.monotonic()
                target.worker.process.kill()
                second = observe(30, 8, first["session"])
                need(
                    second["source"] != first["source"]
                    and second["generation"] > first["generation"],
                    "F1_FAILOVER",
                )
                history = page.evaluate("window.__f1Transitions")
                ready_old = False
                cleared = False
                switched = False
                for item in history:
                    if (
                        item["status"] == "READY"
                        and item["session"] == first["session"]
                    ):
                        need(not switched, "F1_RETIRED_SESSION_RETURNED")
                        ready_old = True
                    if ready_old and item["status"] == "BLOCKED":
                        need(item["rows"] == 0, "F1_BLOCKED_DOM_LEAK")
                        cleared = True
                    if (
                        ready_old
                        and item["status"] == "READY"
                        and item["session"] == second["session"]
                    ):
                        need(cleared, "F1_SWITCH_WITHOUT_CLEAR")
                        switched = True
                need(switched and cleared, "F1_TRANSITION_NOT_OBSERVED")
                result.update(
                    {
                        "dom_matches": len(matched),
                        "failover_dom_cleared": True,
                        "second_source_observed": True,
                        "post_fault_observation_seconds": round(
                            time.monotonic() - t0, 3
                        ),
                    }
                )
            need(not errors, "F1_PAGE_ERRORS")
            browser.close()
        result["status"] = "PASS"
    except Exception as exc:
        result["failure"] = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("F1_")
            else type(exc).__name__
        )
    finally:
        if vite is not None:
            vite.terminate()
            try:
                vite.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vite.kill()
                vite.wait(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        publisher.close()
        for source in sources:
            source.close()
        ledger.close()
    result["ledger"] = audit_ledger(root / "route.jsonl")
    need(not result["ledger"]["torn_tail"], "F1_LEDGER")
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--synthetic", action="store_true")
    m.add_argument("--execute-real-public", action="store_true")
    a = p.parse_args()
    result = run(a.workdir, a.synthetic)
    result["source_sha"] = os.environ.get("GITHUB_SHA", "LOCAL")
    raw = json.dumps(result, sort_keys=True, allow_nan=False).encode()
    with a.output.open("xb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    print(
        "F1_C_RECEIPT",
        result["status"],
        hashlib.sha256(raw).hexdigest(),
        result.get("failure", ""),
        flush=True,
    )
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
