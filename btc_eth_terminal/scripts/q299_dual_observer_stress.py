"""Q299: real isolated observer/collector/IPC/SSE; synthetic source only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from q292_dual_gateway import PRODUCTS, cursor
from q292_isolated_observer import ObserverProcess
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
from q293_epoch_audit import scan
from q293_long_policy import DISK_RESERVE, GIB, process_sample
from q293_source_ledger import SourceLedger
from q293_source_ledger import audit as audit_source_ledger
from q298_capacity_stress import Window

TARGET_BYTES_100MS = 9_709_696
TARGET_BYTES_1S = 14_596_674
TARGET_MSGS_100MS = 70
TARGET_MSGS_1S = 150
TAP_FRAME_CAP = 9_000_000
DURATION = 90


class WitnessObserver(ObserverProcess):
    """Retain only latest witnessed sequence/digest per product, not payloads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.witness = {}
        self.max_ipc_seconds = 0.0

    def observation(self):
        start = time.monotonic()
        value = super().observation()
        self.max_ipc_seconds = max(self.max_ipc_seconds, time.monotonic() - start)
        if value is not None:
            for p, row in value[1].items():
                seq = row["source_sequence"]
                self.witness[p] = (seq, row["source_raw_sha256"])
        return value


def read_receipt(path):
    if path.is_file():
        return json.loads(path.read_bytes())
    return None


def archive_windows(archive, manifest, witnesses):
    windows = [Window(100_000_000), Window(1_000_000_000)]
    wanted = {seq: digest for seq, digest in witnesses.values()}
    seen = {}
    max_frame = count = 0
    oversized = []
    for entry in manifest["chunks"]:
        with (archive / entry["file"]).open("rb") as stream:
            for line in stream:
                item = json.loads(line)
                wire = json.loads(item["raw_text"])
                seq = wire["sequence_num"]
                max_frame = max(max_frame, len(line))
                if seq in wanted:
                    seen[seq] = item["sha256"]
                if len(line) > TAP_FRAME_CAP:
                    oversized.append(seq)
                for w in windows:
                    w.add(int(item["local_monotonic_ns"]), len(line))
                count += 1
    need(seen == wanted and count == sum(c["messages"] for c in manifest["chunks"]),
         "Q299_WITNESS_ARCHIVE_MISMATCH")
    return {"windows": [w.report() for w in windows], "max_frame_bytes": max_frame,
            "oversized_sequences": oversized, "witnesses_matched": len(witnesses)}


def audit_attempts(sources, workers, root, report):
    report["archives"] = {}
    for source in sources:
        for worker in workers[source.name]:
            attempt = worker.work
            archive = capture_path(attempt)
            need(archive is not None, "Q299_ARCHIVE_MISSING")
            path = archive / "manifest.json"
            raw = path.read_bytes()
            manifest = json.loads(raw)
            need(manifest["synthetic_fixture"] is True
                 and manifest["ready_for_research"] is False
                 and manifest["orders_enabled"] is False,
                 "Q299_SYNTHETIC_PROVENANCE")
            pin = hashlib.sha256(raw).hexdigest()
            audited = scan(archive, pin, source.name)
            journal = observer_receipt(attempt / "observer_events.jsonl")
            item = {
                "audit": audited, "manifest": manifest_receipt(path),
                "observer": journal,
                "workload": read_receipt(attempt / "q299_offered.json"),
                "completion": read_receipt(attempt / "q299_done.json"),
                "max_ipc_seconds": round(worker.max_ipc_seconds, 6),
                "windows": archive_windows(archive, manifest, worker.witness),
            }
            report["archives"][f"{source.name}:{attempt.name}"] = item
            need(journal["status"] == "VERIFIED"
                 and audited["zero_gap_certified"] is False
                 and len(audited["epochs"]) == 1
                 and audited["epochs"][0]["both_books_initialized_and_updated"],
                 "Q299_ARCHIVE_AUDIT")
    report["source_ledger"] = audit_source_ledger(root / "source.jsonl")
    report["route_ledger"] = audit_ledger(root / "route.jsonl")
    need(not report["source_ledger"]["torn_tail"]
         and not report["route_ledger"]["torn_tail"], "Q299_LEDGER_TORN")


def evaluate(report, mode):
    archives = report["archives"]
    if mode == "overflow":
        affected = archives[report["injected_source"] + ":attempt_01"]
        offered = affected["workload"]
        need(offered is not None and offered["mode"] == mode
             and offered["oversized_bytes"] > TAP_FRAME_CAP
             and offered["oversized_sequence"] in
             affected["windows"]["oversized_sequences"],
             "Q299_OVERFLOW_NOT_SEALED")
        need(any(e["reason"] == "COLLECTOR_OBSERVATION_SOCKET_CLOSED"
                 for e in affected["observer"]["events"]),
             "Q299_OVERFLOW_NOT_REVOKED")
        return
    checks = {}
    for key, item in archives.items():
        w100, w1 = item["windows"]["windows"]
        checks[key] = {
            "byte_100ms": w100["max_bytes"] >= TARGET_BYTES_100MS,
            "byte_1s": w1["max_bytes"] >= TARGET_BYTES_1S,
            "message_100ms": w100["max_messages"] >= TARGET_MSGS_100MS,
            "message_1s": w1["max_messages"] >= TARGET_MSGS_1S,
            "no_oversized_frame": not item["windows"]["oversized_sequences"],
            "complete_rate_phase": item["completion"] is not None,
            "witnessed_both_products": item["windows"]["witnesses_matched"] == 2,
        }
    report["load_target_checks"] = checks
    need(len(archives) == 2 and all(all(v.values()) for v in checks.values()),
         "Q299_OBSERVED_LOAD_TARGET_NOT_MET")


def run(root, output, mode):
    root.mkdir(mode=0o700)
    report = {"schema": "q299_dual_observer_stress_v1", "status": "FAIL",
              "mode": mode, "synthetic": True, "market_socket_opened": False,
              "real_orders_enabled": False, "zero_gap_certified": False,
              "five_hour_approved": False, "source_sha": os.environ["GITHUB_SHA"],
              "run_id": int(os.environ["GITHUB_RUN_ID"])}
    sources = []
    workers = {p: [] for p in ("primary", "secondary")}
    ledger = source_ledger = publisher = server = clients = thread = None
    identities = set()
    start = time.monotonic()
    peak = max_age = max_poll = 0
    baseline = injected = None
    last = start
    failure = None
    streaming = False
    recovered = None
    try:
        def factory(name, work, duration, synthetic):
            need(synthetic is True, "Q299_REAL_MODE_FORBIDDEN")
            child = WitnessObserver(name, work, duration, synthetic)
            workers[name].append(child)
            return child

        for name in workers:
            sources.append(ManagedSource(
                name, root / name[0], DURATION, True, factory=factory,
                max_restarts=1 if mode == "overflow" else 0))
        ledger = TransitionLedger(root / "route.jsonl")
        source_ledger = SourceLedger(root / "source.jsonl")
        route = ResilientRoute(*sources, source_ledger=source_ledger)
        publisher = Publisher(route, ledger)
        server = BoundedServer(publisher, stream_seconds=120)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        publisher.start()
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        clients = Clients(url, True)
        while time.monotonic() - start < 75:
            now = time.monotonic()
            max_poll = max(max_poll, now - last)
            last = now
            body = publisher.read()
            sample = process_sample(os.getpid())
            identities.update(sample["identity"])
            peak = max(peak, sample["rss_bytes"])
            need(peak <= 3 * GIB and shutil.disk_usage(root).free >= DISK_RESERVE
                 and not publisher.failure and not clients.errors, "Q299_RUNTIME")
            if body["states"] is not None:
                validate(body, True)
                max_age = max(max_age, *(time.time_ns() // 1_000_000
                    - row["source_last_l2_receiver_ms"]
                    for row in body["states"].values()))
            if baseline is None and both_ready(body):
                baseline = cursor(body)
                clients.start()
                streaming = True
            if injected is None and baseline is not None:
                if both_ready(body) and clients.ready(baseline) == CLIENTS:
                    injected = now
                    selected = sources if mode == "burst" else [
                        s for s in sources if s.name == body["active_source"]]
                    report["injected_source"] = (
                        selected[0].name if mode == "overflow" else "both")
                    for s in selected:
                        (s.worker.work / "q299_go").write_text(mode)
                    print("Q299_INJECTION", mode, flush=True)
            if injected is None:
                need(now - start <= 20, "Q299_BASELINE_TIMEOUT")
            elif mode == "burst":
                need(both_ready(body) and cursor(body) == baseline,
                     "Q299_UNPLANNED_DEGRADATION")
                need(all(s.attempts == 1 for s in sources), "Q299_RESTART")
                done = [read_receipt(s.worker.work / "q299_done.json")
                        for s in sources]
                if all(v is not None for v in done) and all(
                    set(s.worker.witness) == set(PRODUCTS)
                    and all(seq >= v["end_sequence"]
                            for seq, _ in s.worker.witness.values())
                    for s, v in zip(sources, done, strict=True)
                ) and clients.ready(baseline) == CLIENTS:
                    recovered = now - injected
                    break
            else:
                if body["states"] is not None and cursor(body) != baseline:
                    need(body["active_source"] != report["injected_source"],
                         "Q299_OVERFLOW_OLD_ACTIVE")
                    stale_cursor_rejected(url, baseline)
                    if both_ready(body) and clients.ready(cursor(body)) == CLIENTS:
                        target = next(s for s in sources
                                      if s.name == report["injected_source"])
                        if target.attempts == 2:
                            recovered = now - injected
                            break
                need(now - injected <= 20, "Q299_OVERFLOW_RECOVERY_TIMEOUT")
            time.sleep(.1)
        need(recovered is not None and min(clients.frames) >= 5,
             "Q299_INCOMPLETE_WORKLOAD")
        report["completion_or_recovery_seconds"] = round(recovered, 6)
    except Exception as exc:
        failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            ("Q299_", "B5_")) else type(exc).__name__
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
                failure = failure or "Q299_CLEANUP"
    report.update({"peak_sampled_tree_rss_bytes": peak,
                   "max_public_state_age_ms": max_age,
                   "max_monitor_poll_seconds": max_poll,
                   "client_frames": clients.frames if clients else []})
    try:
        end = time.monotonic() + 15
        while not process_quiescent(identities) and time.monotonic() < end:
            time.sleep(.1)
        need(process_quiescent(identities), "Q299_PROCESS_LEAK")
        audit_attempts(sources, workers, root, report)
        evaluate(report, mode)
    except Exception as exc:
        failure = failure or (str(exc) if isinstance(exc, ValueError)
                              and str(exc).startswith("Q299_")
                              else type(exc).__name__)
    report["failure"] = failure
    report["status"] = "PASS" if failure is None else "FAIL"
    write_report(output, report)
    print("Q299_" + report["status"], failure or "", flush=True)
    return failure is None


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("burst", "overflow"), required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(0 if run(a.workdir, a.output, a.mode) else 2)
