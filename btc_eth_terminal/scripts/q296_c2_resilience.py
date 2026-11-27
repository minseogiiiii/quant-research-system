"""Bounded C2 resilience pilot and independently gated five-hour capture.

No order placement, no historical stitching, no continuity claim after a fault.
The 30-minute real pilot exercises a standby process loss and active process
loss; the five-hour run observes spontaneous faults only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from q292_dual_gateway import cursor
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger
from q293_b4_diagnostics import manifest_receipt, observer_receipt
from q293_b5_resilience import (
    CLIENTS,
    Clients,
    both_ready,
    process_quiescent,
    stale_cursor_rejected,
    validate,
)
from q293_dual_1800s import capture_path, need, write_report
from q293_epoch_audit import MAX_MESSAGES, MAX_TRADES, TradeIndex, scan
from q293_long_policy import DISK_RESERVE, GIB, admission, process_sample
from q293_source_ledger import SourceLedger
from q293_source_ledger import audit as audit_source_ledger
from q296_c2_policy import RecoveryMonitor

RSS_LIMIT = 3 * GIB


def fixed_reason(exc):
    reason = str(exc)
    if isinstance(exc, ValueError) and reason.startswith(("C2_", "B5_")):
        return reason[:128]
    return type(exc).__name__


def http_check(url, synthetic):
    try:
        with urlopen(url + "/snapshot", timeout=3) as response:
            body = json.load(response)
            need(body["states"] is not None, "C2_EMPTY_HTTP_200")
            validate(body, synthetic)
    except HTTPError as exc:
        with exc:
            body = json.load(exc)
            need(exc.code == 503 and body["states"] is None
                 and body["real_orders_enabled"] is False, "C2_HTTP_QUARANTINE")


def audit_archives(root, sources, synthetic, report):
    index = TradeIndex(root / "trade.db") if not synthetic else None
    started = time.monotonic()
    report["archives"] = {}
    try:
        for source in sources:
            attempts = sorted(source.root.glob("attempt_*"))
            need(len(attempts) == source.attempts, "C2_ARCHIVE_ATTEMPT_INVENTORY")
            for attempt in attempts:
                archive = capture_path(attempt)
                need(archive is not None, "C2_ARCHIVE_MISSING")
                manifest = archive / "manifest.json"
                raw = manifest.read_bytes()
                need(json.loads(raw)["synthetic_fixture"] is synthetic,
                     "C2_ARCHIVE_MODE")
                receipt = manifest_receipt(manifest)
                observer = observer_receipt(attempt / "observer_events.jsonl")
                need(receipt["status"] == "MANIFEST_READ"
                     and observer["status"] == "VERIFIED", "C2_PRIVATE_LEDGER")
                result = scan(archive, hashlib.sha256(raw).hexdigest(),
                              source.name, index)
                need(result["sealed_messages"] > 0
                     and len(result["epochs"]) >= 1
                     and all(epoch["both_books_initialized_and_updated"]
                             for epoch in result["epochs"])
                     and result["zero_gap_certified"] is False,
                     "C2_INDEPENDENT_EPOCH_AUDIT")
                # A killed process may leave an unsealed tail. It is explicitly
                # excluded, never promoted as complete historical data.
                report["archives"][f"{source.name}:{attempt.name}"] = {
                    "audit": result, "manifest": receipt, "observer": observer,
                }
                need(shutil.disk_usage(root).free >= DISK_RESERVE,
                     "C2_AUDIT_DISK_RESERVE")
        if index is not None:
            report["trade_id_crosscheck"] = index.compare()
            report["trade_index_rows"] = index.count
            need(all(v["common_ids"] > 0 and v["divergent_ids"] == 0
                     for v in report["trade_id_crosscheck"].values()),
                 "C2_TRADE_CROSSCHECK")
    finally:
        if index is not None:
            index.close()
        report["audit_seconds"] = round(time.monotonic() - started, 6)


def run(root, output, duration, synthetic, *, inject=False):
    need((duration, synthetic, inject) in (
        (90, True, False), (1800, False, True),
    ), "C2_MODE_DURATION")
    required = admission(180 if synthetic else duration, 1)
    report = {
        "schema": "q296_c2_resilience_v1", "status": "FAIL",
        "duration_seconds": duration, "synthetic": synthetic,
        "source_sha": os.environ.get("GITHUB_SHA", "LOCAL"),
        "run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
        "real_orders_enabled": False, "zero_gap_certified": False,
        "historical_gap_recovered": False, "exchange_attested": False,
        "execution_authorized": False, "c1_clean_baseline_certified": False,
        "five_hour_resilience_certified": False,
        "disk_preflight_required_bytes": required, "injections": [],
    }
    sources = []
    ledger = source_ledger = publisher = server = thread = clients = None
    identities = set()
    failure = None
    monitor = RecoveryMonitor(duration)
    peak_rss = 0
    min_free = shutil.disk_usage(root.parent).free
    checks = 0
    streaming = False
    start = time.monotonic()
    try:
        need(min_free >= required, "C2_DISK_PREFLIGHT")
        root.mkdir(mode=0o700)
        deadline = start + duration + 65
        for name in ("primary", "secondary"):
            sources.append(ManagedSource(name, root / name, duration, synthetic,
                                         deadline=deadline, max_restarts=1))
        source_ledger = SourceLedger(root / "sources.jsonl")
        route = ResilientRoute(*sources, source_ledger=source_ledger)
        ledger = TransitionLedger(root / "route.jsonl")
        publisher = Publisher(route, ledger)
        server = BoundedServer(publisher)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        publisher.start()
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        clients = Clients(url, synthetic)
        while True:
            elapsed = time.monotonic() - start
            body = publisher.read()
            resources = process_sample(os.getpid())
            identities.update(resources["identity"])
            peak_rss = max(peak_rss, resources["rss_bytes"])
            min_free = min(min_free, shutil.disk_usage(root).free)
            need(peak_rss <= RSS_LIMIT and min_free >= DISK_RESERVE,
                 "C2_RUNTIME_RESOURCE_LIMIT")
            need(not publisher.failure and publisher.thread.is_alive()
                 and not clients.errors, "C2_PUBLISHER_OR_SSE_FAILURE")
            if body["states"] is not None:
                validate(body, synthetic)
            old = monitor.observe(elapsed, body)
            if old is not None:
                stale_cursor_rejected(url, old)
            if not streaming and monitor.started is not None:
                clients.start()
                streaming = True
            if streaming and monitor.samples % 20 == 0:
                http_check(url, synthetic)
                checks += 1
            if inject and len(report["injections"]) < 2:
                i = len(report["injections"])
                when = (300, 900)[i]
                if (elapsed >= when and both_ready(body)
                        and clients.ready(cursor(body)) == CLIENTS
                        and monitor.degraded_since is None):
                    label = body["active_source"]
                    if i == 0:
                        label = "secondary" if label == "primary" else "primary"
                    target = next(s for s in sources if s.name == label)
                    need(target.attempts == 1 and target.alive(),
                         "C2_INJECTION_TARGET")
                    report["injections"].append({
                        "source": label, "role": "standby" if i == 0 else "active",
                        "at_seconds": round(elapsed, 6),
                    })
                    target.worker.process.kill()
                else:
                    need(elapsed <= when + 20, "C2_INJECTION_DEADLINE")
            if monitor.samples % 240 == 0:
                print(f"C2_PROGRESS seconds={elapsed:.1f} "
                      f"both={monitor.both_samples} rss={peak_rss}", flush=True)
            if elapsed >= duration:
                report["availability"] = monitor.finish()
                need(not inject or len(report["injections"]) == 2,
                     "C2_MISSING_INJECTION")
                need(streaming and min(clients.frames) >= (20 if synthetic else 500)
                     and not clients.errors and checks >= duration // 10
                     and clients.ready(cursor(body)) == CLIENTS,
                     "C2_CLIENT_ACCEPTANCE")
                break
            time.sleep(.25)
    except Exception as exc:
        failure = fixed_reason(exc)
    finally:
        # Each cleanup action is attempted even when a previous one fails.
        for action in (
            lambda: server.shutdown() if server and thread and thread.is_alive()
            else None,
            lambda: server.server_close() if server else None,
            lambda: thread.join(timeout=3) if thread else None,
            lambda: clients.close() if clients and streaming else None,
            lambda: publisher.close() if publisher else None,
            *(source.close for source in sources),
            lambda: ledger.close() if ledger else None,
            lambda: source_ledger.close() if source_ledger else None,
        ):
            try:
                action()
            except Exception:
                failure = failure or "C2_CLEANUP"
    report.update({
        "peak_rss_bytes": peak_rss, "min_free_disk_bytes": min_free,
        "http_checks": checks, "client_frames": clients.frames if clients else [],
        "client_connections": clients.reconnects if clients else [],
        "observed_seconds": round(monitor.last or 0, 6),
    })
    if root.is_dir():
        try:
            until = time.monotonic() + 15
            while not process_quiescent(identities) and time.monotonic() < until:
                time.sleep(.1)
            need(process_quiescent(identities), "C2_DESCENDANTS_NOT_QUIESCENT")
            report["route_ledger"] = audit_ledger(root / "route.jsonl")
            report["source_ledger"] = audit_source_ledger(root / "sources.jsonl")
            need(report["route_ledger"]["torn_tail"] is False
                 and report["source_ledger"]["torn_tail"] is False
                 and report["source_ledger"]["attempts"] == monitor.attempts,
                 "C2_LEDGER")
            audit_archives(root, sources, synthetic, report)
        except Exception as exc:
            failure = failure or fixed_reason(exc)
    report["failure"] = failure
    report["status"] = "PASS" if failure is None else "FAIL"
    report["five_hour_resilience_certified"] = False
    if failure is None and not synthetic:
        # An empirical capacity estimate with 2x headroom, not long-run proof.
        scale = 2 * 18000 / duration
        max_archive_messages = max(
            v["audit"]["sealed_messages"] for v in report["archives"].values()
        )
        report["capacity_estimate"] = {
            "time_scale_with_margin": scale,
            "projected_max_archive_messages": int(max_archive_messages * scale),
            "projected_trade_index_rows": int(report["trade_index_rows"] * scale),
            "projected_audit_seconds": round(report["audit_seconds"] * scale, 3),
            "required_disk_bytes_at_start": admission(18000, 1),
            "within_configured_caps": (
                max_archive_messages * scale <= MAX_MESSAGES
                and report["trade_index_rows"] * scale <= MAX_TRADES
                and report["audit_seconds"] * scale <= 2400
            ),
            "five_hour_approved": False,
        }
    write_report(output, report)
    print("C2_" + report["status"], failure or "", flush=True)
    return failure is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--execute-real-public-c2-pilot", action="store_true")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Five-hour entry point deliberately absent until pilot qualification,
    # authenticated B5 receipt and capacity envelope have been reviewed.
    duration = 90 if args.synthetic else 1800
    return 0 if run(args.workdir, args.output, duration, args.synthetic,
                    inject=not args.synthetic) else 2


if __name__ == "__main__":
    raise SystemExit(main())
