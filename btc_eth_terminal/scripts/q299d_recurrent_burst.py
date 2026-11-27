"""Q299-D: six measured recurrent dual-source bursts, synthetic offline only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from q292_dual_gateway import PRODUCTS, cursor
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger
from q293_b4_diagnostics import manifest_receipt, observer_receipt
from q293_b5_resilience import CLIENTS, Clients, both_ready, process_quiescent, validate
from q293_dual_1800s import capture_path, need, write_report
from q293_epoch_audit import scan
from q293_long_policy import DISK_RESERVE, GIB, process_sample
from q293_source_ledger import SourceLedger
from q293_source_ledger import audit as audit_source_ledger
from q298_capacity_stress import Window
from q299_dual_observer_stress import (
    TAP_FRAME_CAP,
    TARGET_BYTES_1S,
    TARGET_BYTES_100MS,
    TARGET_MSGS_1S,
    TARGET_MSGS_100MS,
    WitnessObserver,
    read_receipt,
)

CYCLES = 6
DURATION = 240
SETTLE_SECONDS = 10
MAX_RSS_DRIFT = 256 * 1024**2
MAX_MONITOR_GAP = 1.0


def memory_contract(rows):
    need(len(rows) == CYCLES and all(len(r) >= 20 for r in rows),
         "Q299D_INSUFFICIENT_MEMORY_SAMPLES")
    medians = [int(statistics.median(r)) for r in rows]
    drift = max(0, max(medians[1:]) - medians[0])
    return {"settled_median_rss_bytes": medians,
            "settled_samples": [len(r) for r in rows],
            "maximum_increase_after_warmup_bytes": drift,
            "allowed_increase_bytes": MAX_RSS_DRIFT,
            "passed": drift <= MAX_RSS_DRIFT,
            "not_a_proof_of_no_memory_leak": True}


def audit_cycles(archive, manifest, attempts):
    """Each cycle independently meets byte and message windows, exact witness joins."""
    metrics = []
    wanted = {}
    ranges = []
    for i, record in enumerate(attempts, 1):
        offered, done, witnessed = record
        first, last = offered["first_sequence"], done["end_sequence"]
        need(offered["cycle"] == done["cycle"] == i
             and offered["prebuilt"] is True
             and len(offered["frame_bytes"]) == 3
             and offered["first_burst_end"] == first + 2
             and done["rate_messages"] == 2680
             and first <= last and (not ranges or first > ranges[-1][1]),
             "Q299D_RECEIPT_ORDER")
        need(set(witnessed) == set(PRODUCTS)
             and all(seq >= last for seq, _ in witnessed.values()),
             "Q299D_POST_CYCLE_WITNESS")
        for p, (seq, digest) in witnessed.items():
            need((seq, p) not in wanted or wanted[(seq, p)] == digest,
                 "Q299D_WITNESS_CONFLICT")
            wanted[(seq, p)] = digest
        ranges.append((first, last))
        metrics.append({"windows": [Window(100_000_000), Window(1_000_000_000)],
                        "large_sequences": [], "records": 0})
    seen = {}
    position = 0
    records = 0
    for chunk in manifest["chunks"]:
        with (archive / chunk["file"]).open("rb") as stream:
            for line in stream:
                env = json.loads(line)
                wire = json.loads(env["raw_text"])
                seq = wire["sequence_num"]
                records += 1
                need(len(line) <= TAP_FRAME_CAP, "Q299D_OVERSIZE")
                for p in PRODUCTS:
                    if (seq, p) in wanted:
                        need(any(e.get("product_id") == p
                                 for e in wire["events"]),
                             "Q299D_WITNESS_PRODUCT")
                        seen[(seq, p)] = env["sha256"]
                while position < len(ranges) and seq > ranges[position][1]:
                    position += 1
                if position == len(ranges) or seq < ranges[position][0]:
                    continue
                m = metrics[position]
                m["records"] += 1
                if len(line) >= 5_199_000:
                    m["large_sequences"].append(seq)
                for w in m["windows"]:
                    w.add(int(env["local_monotonic_ns"]), len(line))
    need(seen == wanted, "Q299D_WITNESS_HASH")
    need(records == manifest["messages"], "Q299D_ARCHIVE_COUNT")
    result = []
    for i, m in enumerate(metrics):
        first, last = ranges[i]
        w100, w1 = [w.report() for w in m["windows"]]
        checks = {
            "bytes_100ms": w100["max_bytes"] >= TARGET_BYTES_100MS,
            "bytes_1s": w1["max_bytes"] >= TARGET_BYTES_1S,
            "messages_100ms": w100["max_messages"] >= TARGET_MSGS_100MS,
            "messages_1s": w1["max_messages"] >= TARGET_MSGS_1S,
            "three_large_records": m["large_sequences"] == list(range(first, first+3)),
            "sequence_count": m["records"] == last - first + 1,
        }
        result.append({"cycle": i+1, "windows": [w100, w1],
                       "records": m["records"], "checks": checks,
                       "passed": all(checks.values())})
    return result


def run(root, output):
    root.mkdir(mode=0o700)
    report = {"schema": "q299d_recurrent_burst_v1", "status": "FAIL",
              "synthetic": True, "market_socket_opened": False,
              "real_orders_enabled": False, "zero_gap_certified": False,
              "five_hour_approved": False, "cycles_required": CYCLES,
              "source_sha": os.environ["GITHUB_SHA"],
              "run_id": int(os.environ["GITHUB_RUN_ID"]), "cycles": []}
    sources = []
    attempts = {name: [] for name in ("primary", "secondary")}
    workers = {}
    memory = []
    ledger = source_ledger = publisher = server = thread = clients = None
    streaming = False
    identities = set()
    baseline = None
    phase = None
    completed = 0
    failure = None
    peak = max_age = max_gap = 0
    start = last_poll = time.monotonic()
    next_http = start
    http_checks = samples = 0
    try:
        def factory(name, work, duration, synthetic):
            need(synthetic is True and name not in workers,
                 "Q299D_SOURCE_RESTART_OR_REAL_MODE")
            worker = WitnessObserver(name, work, duration, synthetic)
            workers[name] = worker
            return worker

        for name in attempts:
            sources.append(ManagedSource(name, root / name[0], DURATION, True,
                                         factory=factory, max_restarts=0))
        ledger = TransitionLedger(root / "route.jsonl")
        source_ledger = SourceLedger(root / "source.jsonl")
        publisher = Publisher(ResilientRoute(*sources, source_ledger=source_ledger),
                              ledger)
        server = BoundedServer(publisher, stream_seconds=120)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        publisher.start()
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        clients = Clients(url, True)
        while time.monotonic() - start < 210:
            now = time.monotonic()
            gap = now - last_poll
            last_poll = now
            max_gap = max(max_gap, gap)
            body = publisher.read()
            sample = process_sample(os.getpid())
            identities.update(sample["identity"])
            rss = sample["rss_bytes"]
            peak = max(peak, rss)
            samples += 1
            need(peak <= 3*GIB and shutil.disk_usage(root).free >= DISK_RESERVE
                 and not publisher.failure and not clients.errors,
                 "Q299D_RUNTIME")
            if baseline is None and both_ready(body):
                baseline = cursor(body)
                clients.start()
                streaming = True
            if baseline is None:
                need(now - start <= 20, "Q299D_STARTUP")
                time.sleep(.1)
                continue
            if not both_ready(body) or cursor(body) != baseline:
                report["first_degraded"] = {
                    "elapsed_seconds": round(now-start, 6),
                    "status": body.get("status"),
                    "diagnostics": body.get("source_diagnostics"),
                    "attempts": body.get("source_attempts"),
                    "generation": body.get("generation"),
                }
                raise ValueError("Q299D_DEGRADATION")
            need(gap <= MAX_MONITOR_GAP
                 and all(s.attempts == 1 for s in sources)
                 and not clients.resync, "Q299D_SESSION_OR_MONITOR")
            validate(body, True)
            max_age = max(max_age, *(time.time_ns()//1_000_000
                - r["source_last_l2_receiver_ms"] for r in body["states"].values()))
            if now >= next_http:
                with urlopen(url+"/snapshot", timeout=.75) as response:
                    public = json.load(response)
                validate(public, True)
                need(both_ready(public) and cursor(public) == baseline,
                     "Q299D_HTTP_PARITY")
                http_checks += 1
                next_http = now + 2
            if phase is None:
                if clients.ready(baseline) == CLIENTS:
                    number = completed+1
                    phase = {"cycle": number, "start": now,
                             "frames_before": list(clients.frames),
                             "settle_start": None, "memory": [], "peak_rss": rss}
                    for worker in workers.values():
                        (worker.work / f"q299_go_{number}").touch(
                            mode=0o600, exist_ok=False)
                    print("Q299D_CYCLE_INJECTED", number, flush=True)
                elif completed == 0:
                    need(now-start <= 20, "Q299D_CLIENT_STARTUP")
            else:
                phase["peak_rss"] = max(phase["peak_rss"], rss)
                number = phase["cycle"]
                need(now-phase["start"] <= 35, "Q299D_CYCLE_DEADLINE")
                if phase["settle_start"] is None:
                    pending = {}
                    for name, worker in workers.items():
                        offered = read_receipt(
                            worker.work / f"q299_offered_{number}.json")
                        done = read_receipt(worker.work / f"q299_done_{number}.json")
                        witness = worker.witness.copy()
                        if (offered is not None and done is not None
                                and set(witness) == set(PRODUCTS)
                                and all(seq >= done["end_sequence"]
                                        for seq, _ in witness.values())):
                            pending[name] = (offered, done, witness)
                    if len(pending) == 2:
                        for name, entry in pending.items():
                            attempts[name].append(entry)
                        phase["settle_start"] = now
                else:
                    settled = now-phase["settle_start"]
                    if settled >= 5:
                        phase["memory"].append(rss)
                    if settled >= SETTLE_SECONDS and clients.ready(baseline) == CLIENTS:
                        increments = [a-b for a, b in zip(
                            clients.frames, phase["frames_before"], strict=True)]
                        need(min(increments) >= 5, "Q299D_SSE_CYCLE_FRAMES")
                        memory.append(phase["memory"])
                        report["cycles"].append({
                            "cycle": number, "seconds": round(now-phase["start"], 6),
                            "peak_sampled_tree_rss_bytes": phase["peak_rss"],
                            "client_frame_increments": increments,
                        })
                        completed += 1
                        print("Q299D_CYCLE_COMPLETE", completed, flush=True)
                        phase = None
                        if completed == CYCLES:
                            break
            time.sleep(.1)
        need(completed == CYCLES, "Q299D_INCOMPLETE")
        report["memory"] = memory_contract(memory)
        need(report["memory"]["passed"], "Q299D_MEMORY_DRIFT")
    except Exception as exc:
        failure = (str(exc) if isinstance(exc, ValueError)
                   and str(exc).startswith(("Q299D_", "B5_")) else type(exc).__name__)
    finally:
        for action in (
            lambda: clients.close() if streaming else None,
            lambda: server.shutdown() if server and thread and thread.is_alive()
            else None,
            lambda: server.server_close() if server else None,
            lambda: thread.join(timeout=3) if thread else None,
            lambda: publisher.close() if publisher else None,
            *(s.close for s in sources),
            lambda: ledger.close() if ledger else None,
            lambda: source_ledger.close() if source_ledger else None,
        ):
            try:
                action()
            except Exception:
                failure = failure or "Q299D_CLEANUP"
    report.update({"completed_cycles": completed, "samples": samples,
                   "http_checks": http_checks, "peak_sampled_tree_rss_bytes": peak,
                   "max_public_state_age_ms": max_age,
                   "max_monitor_poll_seconds": max_gap,
                   "client_frames": clients.frames if clients else [],
                   "client_reconnects": clients.reconnects if clients else [],
                   "elapsed_seconds": round(time.monotonic()-start, 6)})
    report["archives"] = {}
    try:
        deadline = time.monotonic()+15
        while not process_quiescent(identities) and time.monotonic() < deadline:
            time.sleep(.1)
        need(process_quiescent(identities), "Q299D_PROCESS_LEAK")
        need(not clients.errors and not clients.resync, "Q299D_CLIENT_ERROR")
        for name, worker in workers.items():
            archive = capture_path(worker.work)
            need(archive is not None, "Q299D_ARCHIVE_MISSING")
            raw = (archive/"manifest.json").read_bytes()
            manifest = json.loads(raw)
            need(manifest["synthetic_fixture"] is True
                 and manifest["ready_for_research"] is False
                 and manifest["orders_enabled"] is False
                 and manifest["faults"] == [], "Q299D_SOURCE_INTEGRITY")
            audit = scan(archive, hashlib.sha256(raw).hexdigest(), name)
            journal = observer_receipt(worker.work/"observer_events.jsonl")
            cycles = audit_cycles(archive, manifest, attempts[name])
            report["archives"][name] = {
                "audit": audit, "manifest": manifest_receipt(archive/"manifest.json"),
                "observer": journal, "cycles": cycles,
                "max_ipc_seconds": round(worker.max_ipc_seconds, 6)}
            need(journal["status"] == "VERIFIED"
                 and len(audit["epochs"]) == 1
                 and audit["epochs"][0]["both_books_initialized_and_updated"]
                 and audit["unsealed_entries_excluded"] == 0
                 and len(cycles) == CYCLES, "Q299D_ARCHIVE_INTEGRITY")
        report["source_ledger"] = audit_source_ledger(root/"source.jsonl")
        report["route_ledger"] = audit_ledger(root/"route.jsonl")
        need(len(report["archives"]) == 2
             and not report["source_ledger"]["torn_tail"]
             and not report["route_ledger"]["torn_tail"], "Q299D_LEDGER")
        need(all(c["passed"] for a in report["archives"].values()
                 for c in a["cycles"]), "Q299D_CYCLE_AUDIT")
    except Exception as exc:
        failure = failure or (str(exc) if isinstance(exc, ValueError)
                              and str(exc).startswith("Q299D_")
                              else type(exc).__name__)
    report["failure"] = failure
    report["status"] = "PASS" if failure is None else "FAIL"
    write_report(output, report)
    print("Q299D_"+report["status"], failure or "", flush=True)
    return failure is None


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(0 if run(a.workdir, a.output) else 2)
