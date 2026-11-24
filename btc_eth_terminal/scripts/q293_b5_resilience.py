"""B5 controlled local process loss: bounded dual read-only source recovery."""
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
from urllib.request import Request, urlopen

from q28_transport_incident_receipt import incident_receipt
from q292_dual_gateway import PRODUCTS, cursor
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger
from q293_b4_diagnostics import manifest_receipt, observer_receipt
from q293_dual_1800s import capture_path, need, write_report
from q293_epoch_audit import TradeIndex, scan
from q293_long_policy import DISK_RESERVE, GIB, admission, process_sample
from q293_source_ledger import SourceLedger
from q293_source_ledger import audit as audit_source_ledger

LIMIT = 256 * 1024
RECOVERY_LIMIT = 20
CLIENTS = 8


def validate(body, synthetic):
    need(body["real_orders_enabled"] is False
         and body["zero_gap_certified"] is False
         and body.get("historical_gap_recovered") is False
         and body.get("execution_authorized") is False
         and body.get("exchange_attested") is False, "B5_AUTHORITY")
    if body["states"] is None:
        return
    need(body["status"] == "HEALTHY" and set(body["states"]) == set(PRODUCTS)
         and body["mode"] == ("SYNTHETIC_ONLY" if synthetic else
                              "LIVE_READ_ONLY_CANDIDATE"), "B5_MODE")
    for p, row in body["states"].items():
        need(row["product"] == p and row["session_id"] == body["source_session"]
             and row["execution_authorized"] is False
             and row["trading_enabled"] is False
             and row["live_feed"] is (not synthetic)
             and row["mode"] == ("ARCHIVE_SIMULATION" if synthetic else "LIVE_SOURCE")
             and 0 <= time.time_ns() // 1_000_000 -
             row["source_last_l2_receiver_ms"] <= 3500, "B5_STALE_OR_EPOCH")


class Clients:
    """Eight independent consuming clients; no buffering across a cursor change."""

    def __init__(self, url, synthetic):
        self.url, self.synthetic = url, synthetic
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.bound = {}
        self.resync = {}
        self.frames = [0] * CLIENTS
        self.reconnects = [0] * CLIENTS
        self.errors = []
        self.threads = [threading.Thread(target=self.read, args=(i,), daemon=True)
                        for i in range(CLIENTS)]

    def start(self):
        for t in self.threads:
            t.start()

    def read(self, i):
        while not self.stop.is_set():
            bound = None
            try:
                with urlopen(self.url + "/events", timeout=5) as response:
                    with self.lock:
                        self.reconnects[i] += 1
                    while not self.stop.is_set():
                        line = response.readline(LIMIT + 1)
                        if not line:
                            break
                        if line.startswith(b":"):
                            need(response.readline(3) == b"\n", "B5_SSE_HEARTBEAT")
                            continue
                        need(len(line) < 256 and line.startswith(b"id: "),
                             "B5_SSE_ID")
                        received = line[4:].decode().strip()
                        event = response.readline(64)
                        data = response.readline(LIMIT + 1)
                        need(len(data) <= LIMIT and data.startswith(b"data: ")
                             and response.readline(3) == b"\n", "B5_SSE_FRAME")
                        body = json.loads(data[6:])
                        need(received == cursor(body), "B5_SSE_CURSOR")
                        if event == b"event: resync_required\n":
                            need(body["states"] is None, "B5_RESYNC_LEAK")
                            with self.lock:
                                if bound is not None:
                                    self.resync.setdefault(bound, set()).add(i)
                            break
                        need(event == b"event: snapshot\n", "B5_SSE_EVENT")
                        validate(body, self.synthetic)
                        need(body["states"] is not None, "B5_EMPTY_SNAPSHOT")
                        need(bound in (None, received), "B5_CROSS_EPOCH_STREAM")
                        bound = received
                        with self.lock:
                            self.bound[i] = bound
                            self.frames[i] += 1
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code != 503 and not self.stop.is_set():
                    with self.lock:
                        self.errors.append("HTTP_" + str(code))
                    return
            except (OSError, TimeoutError):
                # Transport loss clears the cursor; retry with a fresh snapshot.
                pass
            except (ValueError, KeyError, TypeError):
                with self.lock:
                    self.errors.append("SSE_CONTRACT")
                return
            finally:
                with self.lock:
                    self.bound.pop(i, None)
            self.stop.wait(.1)

    def ready(self, key):
        with self.lock:
            return sum(v == key for v in self.bound.values())

    def resynced(self, key):
        with self.lock:
            return len(self.resync.get(key, set()))

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=6)
        need(not any(t.is_alive() for t in self.threads), "B5_CLIENT_STOP")


def stale_cursor_rejected(url, old):
    try:
        with urlopen(Request(url + "/snapshot", headers={"Last-Event-ID": old}),
                     timeout=3):
            raise ValueError("B5_STALE_CURSOR_ACCEPTED")
    except HTTPError as exc:
        with exc:
            need(exc.code == 409 and json.load(exc)["states"] is None,
                 "B5_STALE_CURSOR_RESPONSE")


def both_ready(body):
    return (body["states"] is not None
            and set(body.get("checkpoint_comparison", {})) == set(PRODUCTS)
            and set(body["checkpoint_comparison"].values())
            <= {"MATCH", "NOT_COMPARABLE"})


def process_quiescent(identities):
    for pid, start in identities:
        if pid == os.getpid():
            continue
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[19]) == start and fields[0] != "Z":
                return False
        except (OSError, ValueError, IndexError):
            continue
    return True


def run(root, output, synthetic):
    duration = 90 if synthetic else 1800
    schedule = (15, 45) if synthetic else (300, 900)
    required = admission(180 if synthetic else 1800, 1)
    need(shutil.disk_usage(root.parent).free >= required,
         "B5_DISK_PREFLIGHT")
    root.mkdir(mode=0o700)
    report = {"schema": "q293_b5_resilience_v1", "status": "FAIL",
              "duration_seconds": duration, "synthetic": synthetic,
              "source_sha": os.environ.get("GITHUB_SHA", "LOCAL"),
              "run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
              "real_orders_enabled": False, "zero_gap_certified": False,
              "historical_gap_recovered": False, "resilience_5h_certified": False,
              "exchange_attested": False, "faults": [], "archives": {}}
    sources = []
    publisher = server = thread = clients = ledger = source_ledger = None
    identities = set()
    failure = None
    pending = None
    baseline = None
    recovered = []
    retired = set()
    samples = unavailable = 0
    peak_rss = max_poll = 0
    min_free = shutil.disk_usage(root).free
    start = time.monotonic()
    previous_poll = start
    both_samples = 0
    try:
        deadline = start + duration + 65
        for name in ("primary", "secondary"):
            sources.append(ManagedSource(name, root / name, duration, synthetic,
                                         deadline=deadline, max_restarts=1))
        source_ledger = SourceLedger(root / "source.jsonl")
        route = ResilientRoute(*sources, source_ledger=source_ledger)
        ledger = TransitionLedger(root / "route.jsonl")
        publisher = Publisher(route, ledger)
        server = BoundedServer(publisher, stream_seconds=120)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        publisher.start()
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_address[1])
        clients = Clients(url, synthetic)
        # Start streaming only after the first valid route exists.
        expected = {"primary": 1, "secondary": 1}
        while time.monotonic() - start < duration:
            now = time.monotonic()
            elapsed = now - start
            max_poll = max(max_poll, now - previous_poll)
            previous_poll = now
            body = publisher.read()
            samples += 1
            resources = process_sample(os.getpid())
            identities.update(resources["identity"])
            peak_rss = max(peak_rss, resources["rss_bytes"])
            min_free = min(min_free, shutil.disk_usage(root).free)
            need(peak_rss <= 3 * GIB and min_free >= DISK_RESERVE
                 and not publisher.failure and not clients.errors, "B5_RUNTIME")
            if body["states"] is None:
                unavailable += 1
                need(pending is not None or baseline is None, "B5_UNPLANNED_OUTAGE")
            else:
                validate(body, synthetic)
                current = cursor(body)
                need(current not in retired, "B5_RETIRED_CURSOR_RETURNED")
                if baseline is None and both_ready(body):
                    baseline = current
                    clients.start()
                if baseline is not None and pending is None:
                    need(current == baseline, "B5_UNPLANNED_TRANSITION")
                if both_ready(body):
                    both_samples += 1
                if pending is not None:
                    t = now - pending["at"]
                    if current != pending["cursor"] and pending["promoted"] is None:
                        need(body["active_source"] != pending["source"]
                             and body["generation"] > pending["generation"]
                             and body["source_session"] != pending["session"],
                             "B5_INVALID_FAILOVER")
                        pending["promoted"] = t
                        pending["new_cursor"] = current
                        retired.add(pending["cursor"])
                        stale_cursor_rejected(url, pending["cursor"])
                    elif pending["promoted"] is not None:
                        need(current == pending["new_cursor"], "B5_EXTRA_TRANSITION")
                    if (pending["promoted"] is not None and both_ready(body)
                            and body["source_attempts"] == expected
                            and clients.ready(current) == CLIENTS
                            and clients.resynced(pending["cursor"]) == CLIENTS):
                        report["faults"][-1].update({
                            "promotion_seconds": round(pending["promoted"], 3),
                            "recovery_seconds": round(t, 3),
                            "resync_clients": CLIENTS, "stale_cursor_rejected": True,
                            "fresh_standby_observed": True,
                            "new_generation": body["generation"]})
                        baseline = current
                        recovered.append(pending["source"])
                        pending = None
                if (pending is None and baseline is not None
                        and len(recovered) < len(schedule)
                        and elapsed >= schedule[len(recovered)] and both_ready(body)
                        and clients.ready(current) == CLIENTS):
                    label = body["active_source"]
                    need(label not in recovered, "B5_WRONG_FAULT_TARGET")
                    target = next(s for s in sources if s.name == label)
                    worker = target.worker
                    need(worker is not None and worker.process.poll() is None,
                         "B5_TARGET_NOT_ALIVE")
                    expected[label] += 1
                    pending = {"at": time.monotonic(), "cursor": current,
                               "source": label, "session": body["source_session"],
                               "generation": body["generation"], "promoted": None,
                               "new_cursor": None}
                    report["faults"].append({"source": label,
                                             "at_seconds": round(elapsed, 3),
                                             "old_generation": body["generation"]})
                    worker.process.kill()
            if pending is not None:
                need(now - pending["at"] <= RECOVERY_LIMIT, "B5_RECOVERY_TIMEOUT")
                if pending["promoted"] is None:
                    need(now - pending["at"] <= 5, "B5_PROMOTION_TIMEOUT")
            elif baseline is None:
                need(elapsed < 20, "B5_STARTUP_TIMEOUT")
            elif len(recovered) < 2:
                need(elapsed <= schedule[len(recovered)] + 20,
                     "B5_INJECTION_NOT_ARMED")
            for source in sources:
                need(source.attempts <= expected[source.name], "B5_EXTRA_ATTEMPT")
            if baseline is not None and pending is None and elapsed >= 20:
                need(both_ready(body), "B5_UNPLANNED_STANDBY_LOSS")
            if samples % 240 == 0:
                print(f"B5_PROGRESS seconds={elapsed:.1f} recovered={len(recovered)} "
                      f"samples={samples} rss={peak_rss}", flush=True)
            time.sleep(.25)
        need(len(recovered) == 2 and pending is None
             and set(recovered) == {"primary", "secondary"}
             and all(s.attempts == 2 for s in sources)
             and min(clients.frames) >= (20 if synthetic else 500)
             and max_poll <= 5 and both_samples >= (100 if synthetic else 5000),
             "B5_RUNTIME_ACCEPTANCE")
    except Exception as exc:
        failure = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(
            "B5_") else type(exc).__name__
    finally:
        # Close gateway before stopping archives; never publish a shutdown quote.
        for action in (
            lambda: server.shutdown() if server else None,
            lambda: server.server_close() if server else None,
            lambda: thread.join(timeout=3) if thread else None,
            lambda: clients.close() if clients and baseline is not None else None,
            lambda: publisher.close() if publisher else None,
            *(s.close for s in sources),
            lambda: ledger.close() if ledger else None,
            lambda: source_ledger.close() if source_ledger else None,
        ):
            try:
                action()
            except Exception:
                failure = failure or "B5_CLEANUP"
    report.update({"samples": samples, "unavailable_samples": unavailable,
                   "both_ready_samples": both_samples,
                   "observed_seconds": round(time.monotonic() - start, 3),
                   "peak_rss_bytes": peak_rss, "min_free_disk_bytes": min_free,
                   "max_poll_gap_seconds": round(max_poll, 3),
                   "client_frames": clients.frames if clients else [],
                   "client_connections": clients.reconnects if clients else []})
    try:
        until = time.monotonic() + 15
        while not process_quiescent(identities) and time.monotonic() < until:
            time.sleep(.1)
        need(process_quiescent(identities), "B5_DESCENDANT_NOT_QUIESCENT")
        # Preserve independent bounded close/guard evidence before archive
        # semantic replay, which may itself fail. No peer intent is inferred.
        report["transport_incidents"] = {}
        for source in sources:
            for attempt in sorted(source.root.glob("attempt_*")):
                key = source.name + ":" + attempt.name
                report["transport_incidents"][key] = incident_receipt(
                    attempt / "ws.jsonl", attempt / "hb.jsonl",
                    collector_failed=False)
        report["route_ledger"] = audit_ledger(root / "route.jsonl")
        report["source_ledger"] = audit_source_ledger(root / "source.jsonl")
        index = TradeIndex(root / "trade.db") if not synthetic else None
        try:
            for source in sources:
                for attempt in sorted(source.root.glob("attempt_*")):
                    key = source.name + ":" + attempt.name
                    archive = capture_path(attempt)
                    need(archive is not None, "B5_ARCHIVE_MISSING")
                    manifest = archive / "manifest.json"
                    raw = manifest.read_bytes()
                    need(json.loads(raw)["synthetic_fixture"] is synthetic,
                         "B5_ARCHIVE_MODE")
                    result = scan(archive, hashlib.sha256(raw).hexdigest(),
                                  source.name, index)
                    report["archives"][key] = {
                        "audit": result, "faults": manifest_receipt(manifest),
                        "observer": observer_receipt(attempt / "observer_events.jsonl")}
            if index is not None:
                report["trade_id_crosscheck"] = index.compare()
        finally:
            if index is not None:
                index.close()
        need(len(report["archives"]) == 4
             and all(a["audit"]["sealed_messages"] > 0
                     and len(a["audit"]["epochs"]) == 1
                     and a["audit"]["epochs"][0]["both_books_initialized_and_updated"]
                     and a["audit"]["zero_gap_certified"] is False
                     for a in report["archives"].values()), "B5_EPOCH_ACCEPTANCE")
        need(report["route_ledger"]["records"] >= 3
             and report["route_ledger"]["torn_tail"] is False
             and report["source_ledger"]["torn_tail"] is False
             and report["source_ledger"]["attempts"] == {"primary": 2, "secondary": 2},
             "B5_LEDGER")
        if not synthetic:
            need(all(v["common_ids"] > 0 and v["divergent_ids"] == 0
                     for v in report["trade_id_crosscheck"].values()), "B5_TRADE")
    except Exception as exc:
        failure = failure or (str(exc) if isinstance(exc, ValueError)
                              and str(exc).startswith("B5_") else type(exc).__name__)
    report["status"] = "PASS" if failure is None else "FAIL"
    report["failure"] = failure
    write_report(output, report)
    print("B5_" + report["status"], failure or "", flush=True)
    return failure is None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--execute-real-public-b5", action="store_true")
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    raise SystemExit(0 if run(args.workdir, args.output, args.synthetic) else 2)


if __name__ == "__main__":
    main()
