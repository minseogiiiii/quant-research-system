"""Q29.3-B4: actual dual Coinbase 30-minute clean baseline, never synthetic.

Clean dual-source admission requires separate sealed archives, independent
replays, complete close diagnostics and observed HTTP/SSE freshness. No
claim of 30m/5h resilience or continuous exchange-signed history.
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
from urllib.request import urlopen

from q22_ci_full_replay import _book_agreement, _worker_run
from q24_ws_diagnostic_receipt import build_receipt as ws_receipt
from q27_heartbeat_receipt import build_receipt as hb_receipt
from q28_transport_incident_receipt import incident_receipt
from q292_dual_gateway import PRODUCTS
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger
from q293_epoch_audit import TradeIndex, scan
from q293_long_policy import DISK_RESERVE, GIB, admission, budgets, process_sample
from q293_source_ledger import SourceLedger
from q293_source_ledger import audit as audit_source_ledger
from v36_3_durable_archive_gate import audit as archive_audit

DURATION = 1800
MIN_FREE = admission(DURATION)
RSS_LIMIT = 3 * GIB


def need(ok, code):
    if not ok:
        raise ValueError(code)


def write_report(path, report):
    with path.open("x") as out:
        json.dump(report, out, sort_keys=True, indent=2, allow_nan=False)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def capture_path(attempt):
    found = list((attempt / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
    if len(found) != 1:
        return None
    return found[0]


def completed(attempt):
    source = capture_path(attempt)
    if source is None:
        return False
    try:
        manifest = json.loads((source / "manifest.json").read_bytes())
        return manifest.get("finish_reason") == "DURATION_REACHED"
    except (OSError, ValueError):
        return False


def check_archive(attempt):
    source = capture_path(attempt)
    need(source is not None, "PILOT_MISSING_ARCHIVE")
    manifest_path = source / "manifest.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    pin = hashlib.sha256(raw).hexdigest()
    need(manifest["required_duration_seconds"] == DURATION
         and manifest.get("synthetic_fixture") is False
         and manifest.get("orders_enabled") is False
         and manifest.get("finish_reason") == "DURATION_REACHED"
         and manifest.get("total_reconnects") == 0
         and manifest.get("faults") == [], "PILOT_ARCHIVE_NOT_CLEAN")
    gate = archive_audit(source, pin)
    need(gate["eligible_for_archived_replay"] is True
         and gate["source_connections"] == 1
         and gate["monotonic_source_span_seconds"] >= DURATION - 5,
         "PILOT_DURABLE_ARCHIVE_REJECTED")
    started = time.monotonic()
    main = _worker_run(source, pin, "primary")["data"]
    independent = _worker_run(source, pin, "independent")["data"]
    replay_seconds = time.monotonic() - started
    _book_agreement(main, independent)
    for result in (main, independent):
        need(result["messages"] == gate["sealed_messages_verified"]
             and result["chunks"] == gate["sealed_chunks_verified"]
             and result["connections"] == 1, "PILOT_REPLAY_COUNT_MISMATCH")
    ws = ws_receipt(attempt / "ws.jsonl")
    hb = hb_receipt(attempt / "hb.jsonl")
    close = incident_receipt(attempt / "ws.jsonl", attempt / "hb.jsonl",
                             collector_failed=False)
    need(ws["trace_schema_verified"] is True and ws["probe_exited"] is True
         and ws["observed_connections"] == 1
         and ws["websocket_error_events"] == 0
         and hb["trace_schema_verified"] is True
         and hb["synthetic_fixture"] is False
         and hb["collector_stop_observed"] is True
         and hb["opened_sockets_constructed"] == 1
         and hb["interventions_total"] == 0
         and hb["heartbeat_timeout_events"] == 0
         and ws["last_reported_message_count"] == independent["messages"]
         and ws["last_reported_heartbeat_count"] == hb["last_heartbeat_count"]
         and close["observed_termination_class"] ==
         "LOCAL_NON_GUARD_CLOSE_OBSERVED", "PILOT_FORENSICS_REJECTED")
    return {"messages": independent["messages"],
            "chunks": independent["chunks"], "bytes": gate["sealed_bytes_verified"],
            "source_span_seconds": gate["monotonic_source_span_seconds"],
            "manifest_sha256": pin, "independent_replay_agrees": True,
            "two_replays_seconds": round(replay_seconds, 3),
            "independent_books_sha256": {
                p: independent["products"][p]["sha256"] for p in PRODUCTS},
            "close_trace_verified": True}


def validate_snapshot(body):
    need(body["mode"] == "LIVE_READ_ONLY_CANDIDATE"
         and body["execution_authorized"] is False
         and body["real_orders_enabled"] is False
         and body["zero_gap_certified"] is False
         and body["exchange_attested"] is False
         and set(body["states"]) == set(PRODUCTS), "PILOT_PUBLIC_SCHEMA")
    for p, row in body["states"].items():
        need(row["product"] == p and row["session_id"] == body["source_session"]
             and row["mode"] == "LIVE_SOURCE"
             and row["live_feed"] is True and row["trading_enabled"] is False
             and row["execution_authorized"] is False,
             "PILOT_PUBLIC_EPOCH")


def run(root, output):
    need(shutil.disk_usage(root.parent).free >= MIN_FREE, "PILOT_DISK_PREFLIGHT")
    root.mkdir(mode=0o700)
    sources = []
    ledger = source_ledger = publisher = server = thread = None
    report = {"schema": "q293_dual_1800s_baseline_v1", "status": "FAIL",
              "duration_seconds": DURATION, "source_sha": os.environ["GITHUB_SHA"],
              "run_id": int(os.environ["GITHUB_RUN_ID"]),
              "real_orders_enabled": False, "five_hour_certified": False,
              "resilience_5h_certified": False, "exchange_attested": False}
    samples = good = unavailable = 0
    both_ready_samples = http_sse_checks = 0
    peak_rss, min_free = 0, shutil.disk_usage(root).free
    peak_processes = 0
    initial_free = min_free
    cpu_seconds = 0.0
    last_sample = None
    max_poll_gap = 0.0
    first_good = last_good = None
    raw_cap, archive_cap = budgets(DURATION)
    comparison = {"MATCH": 0, "NOT_COMPARABLE": 0,
                  "SOURCE_UNAVAILABLE": 0, "DIVERGENCE": 0}
    failure = None
    try:
        deadline = time.monotonic() + DURATION + 65
        for name in ("primary", "secondary"):
            sources.append(ManagedSource(name, root / name, DURATION, False,
                                         deadline=deadline, max_restarts=0))
        source_ledger = SourceLedger(root / "sources.jsonl")
        route = ResilientRoute(*sources, source_ledger=source_ledger)
        ledger = TransitionLedger(root / "transitions.jsonl")
        publisher = Publisher(route, ledger)
        server = BoundedServer(publisher)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        publisher.start()
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_address[1])
        start = time.monotonic()
        baseline = None
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - start
            body = publisher.read()
            samples += 1
            resources = process_sample(os.getpid())
            peak_rss = max(peak_rss, resources["rss_bytes"])
            peak_processes = max(peak_processes, resources["process_count"])
            cpu_seconds = max(cpu_seconds, resources["cpu_seconds_live_tree"])
            min_free = min(min_free, shutil.disk_usage(root).free)
            if last_sample is not None:
                max_poll_gap = max(max_poll_gap, elapsed - last_sample)
            last_sample = elapsed
            need(min_free >= DISK_RESERVE and peak_rss <= RSS_LIMIT,
                 "PILOT_RESOURCE_LIMIT")
            need(not publisher.failure and publisher.thread.is_alive(),
                 "PILOT_PUBLISHER_FAILURE")
            if body["states"] is not None:
                validate_snapshot(body)
                good += 1
                first_good = elapsed if first_good is None else first_good
                last_good = elapsed
                if 20 <= elapsed <= DURATION - 8:
                    need(resources["process_count"] >= 5,
                         "PILOT_PROCESS_TREE_INCOMPLETE")
                    need("SOURCE_UNAVAILABLE" not in
                         body["checkpoint_comparison"].values(),
                         "PILOT_STANDBY_UNAVAILABLE")
                    both_ready_samples += 1
                if baseline is None:
                    baseline = (body["generation"], body["source_session"])
                need((body["generation"], body["source_session"]) == baseline,
                     "PILOT_BASELINE_GENERATION_CHANGED")
                for value in body["checkpoint_comparison"].values():
                    comparison[value] += 1
                need("DIVERGENCE" not in body["checkpoint_comparison"].values(),
                     "PILOT_CROSS_SOURCE_DIVERGENCE")
            elif 20 <= elapsed <= DURATION - 8:
                unavailable += 1
            # Verify the actual public HTTP and SSE paths while both run.
            if 20 <= elapsed <= DURATION - 8 and samples % 20 == 0:
                with urlopen(url + "/snapshot", timeout=3) as response:
                    validate_snapshot(json.load(response))
                with urlopen(url + "/events", timeout=3) as response:
                    need(response.readline().startswith(b"id:")
                         and response.readline() == b"event: snapshot\n",
                         "PILOT_SSE_SNAPSHOT_MISSING")
                    validate_snapshot(json.loads(response.readline()[6:]))
                http_sse_checks += 1
            need(all(source.attempts <= 1 for source in sources),
                 "PILOT_BASELINE_RESTARTED")
            if elapsed >= DURATION - 5 and all(
                completed(source.root / "attempt_01") for source in sources
            ):
                break
            if samples % 240 == 0:
                print(f"B4_PROGRESS seconds={elapsed:.1f} samples={samples} "
                      f"rss={peak_rss} free={min_free}", flush=True)
            time.sleep(.25)
        need(time.monotonic() - start >= DURATION - 5
             and all(completed(source.root / "attempt_01") for source in sources),
             "PILOT_FULL_CAPTURE_NOT_COMPLETED")
        need(good >= 3000 and unavailable == 0 and both_ready_samples >= 3000
             and http_sse_checks >= 100 and max_poll_gap <= 5
             and first_good is not None and last_good - first_good >= 1770
             and peak_processes >= 5, "PILOT_PUBLIC_AVAILABILITY")
    except Exception as exc:
        # Fixed diagnostic labels only; never serialize private prices/paths.
        failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            "PILOT_") else type(exc).__name__
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
        if publisher is not None:
            publisher.close()
        for source in sources:
            source.close()
        if ledger is not None:
            ledger.close()
        if source_ledger is not None:
            source_ledger.close()
    report.update({"poll_samples": samples, "healthy_samples": good,
                   "unavailable_middle_samples": unavailable,
                   "peak_process_tree_rss_bytes": peak_rss,
                   "min_free_disk_bytes": min_free,
                   "checkpoint_comparison_counts": comparison,
                   "both_ready_samples": both_ready_samples,
                   "http_sse_checks": http_sse_checks,
                   "peak_process_count": peak_processes,
                   "max_poll_gap_seconds": round(max_poll_gap, 3),
                   "healthy_span_seconds": round(last_good - first_good, 3)
                   if first_good is not None else 0,
                   "cpu_seconds_live_tree_max_sample": round(cpu_seconds, 3),
                   "disk_consumed_peak_bytes": initial_free - min_free,
                   "per_source_raw_cap_bytes": raw_cap,
                   "per_source_archive_cap_bytes": archive_cap,
                   "disk_preflight_required_bytes": MIN_FREE})
    try:
        if ledger is not None:
            report["ledger"] = audit_ledger(root / "transitions.jsonl")
        if source_ledger is not None:
            report["source_ledger"] = audit_source_ledger(root / "sources.jsonl")
        if failure is None:
            report["archives"] = {
                source.name: check_archive(source.root / "attempt_01")
                for source in sources}
            need(len(report["archives"]) == 2
                 and report["ledger"]["torn_tail"] is False,
                 "PILOT_INCOMPLETE_EVIDENCE")
            need(report["source_ledger"]["torn_tail"] is False
                 and report["source_ledger"]["attempts"] ==
                 {"primary": 1, "secondary": 1}, "PILOT_SOURCE_LEDGER")
            for counts in report["source_ledger"]["states"].values():
                need(counts.get("READY") == 1, "PILOT_SOURCE_EPOCH_CHANGED")
            report["status"] = "ARCHIVES_PASS"
    except Exception as exc:
        failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            "PILOT_") else type(exc).__name__
    # Even on failed capture, preserve bounded sealed-prefix evidence.
    report["epoch_audits"] = {}
    try:
        index = TradeIndex(root / "trade_index.db")
        try:
            for source in sources:
                for attempt in sorted(source.root.glob("attempt_*")):
                    archive = capture_path(attempt)
                    key = source.name + ":" + attempt.name
                    if archive is None:
                        report["epoch_audits"][key] = {"status": "NO_ARCHIVE"}
                        continue
                    try:
                        pin = hashlib.sha256((archive / "manifest.json").read_bytes())
                        value = scan(archive, pin.hexdigest(), source.name, index)
                        report["epoch_audits"][key] = value
                    except Exception as exc:
                        report["epoch_audits"][key] = {"status": "REJECTED",
                                                       "reason": type(exc).__name__}
            report["trade_id_crosscheck"] = index.compare()
        finally:
            index.close()
        if failure is None:
            need(len(report["epoch_audits"]) == 2, "PILOT_EPOCH_COUNT")
            for key, value in report["epoch_audits"].items():
                name = key.split(":")[0]
                need(value.get("schema") == "q293_sealed_epoch_forensics_v1"
                     and value["unsealed_entries_excluded"] == 0
                     and value["manifest_tmp_excluded"] is False
                     and value["sealed_messages"] == report["archives"][name]["messages"]
                     and value["manifest_sha256"] ==
                     report["archives"][name]["manifest_sha256"]
                     and len(value["epochs"]) == 1
                     and value["epochs"][0]["both_books_initialized_and_updated"]
                     and value["epochs"][0]["book_sha256"] ==
                     report["archives"][name]["independent_books_sha256"],
                     "PILOT_EPOCH_AUDIT")
            for result in report["trade_id_crosscheck"].values():
                need(result["common_ids"] > 0 and result["divergent_ids"] == 0,
                     "PILOT_TRADE_CROSSCHECK")
            report["status"] = "PASS"
    except Exception as exc:
        if failure is None:
            failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
                "PILOT_") else type(exc).__name__
    if failure is not None:
        report["status"] = "FAIL"
        report["failure"] = failure
        report["incident_receipts"] = {}
        for source in sources:
            attempt = source.root / "attempt_01"
            report["incident_receipts"][source.name] = incident_receipt(
                attempt / "ws.jsonl", attempt / "hb.jsonl")
    write_report(output, report)
    print("Q293_DUAL_1800S_" + report["status"], flush=True)
    return 0 if report["status"] == "PASS" else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute-real-public-1800s", action="store_true", required=True)
    args = p.parse_args()
    return run(args.workdir, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
