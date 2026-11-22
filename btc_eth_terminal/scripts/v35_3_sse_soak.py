"""Real archived Coinbase SSE process soak and crash/restart provenance audit.

Finite archive replay only. No live exchange connection or orders. Local loopback.
The evidence is this bounded run, NOT 24h or live-source silence certification.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    from v35_replay_service import replay_capture
    return replay_capture

replay_capture = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

CLEAN = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
SECOND = (
    ROOT
    / "backend/data/live_disk_safe_final_180s_20261114/coinbase_btc_eth_soak_rV6Xox"
)


def pin(root):
    return sha256((root / "manifest.json").read_bytes()).hexdigest()


def endpoint(base, path, headers=None, timeout=5):
    before = time.monotonic()
    try:
        with urlopen(Request(base + path, headers=headers or {}), timeout=timeout) as r:
            status, raw = r.status, r.read()
    except HTTPError as e:
        status, raw = e.code, e.read()
    return status, raw, (time.monotonic() - before) * 1000


def events(raw):
    payload = []
    for chunk in raw.decode("utf8").split("\n\n"):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        ev = {}
        for line in lines:
            if line.startswith("event: "):
                ev["name"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
            elif line.startswith("id: "):
                ev["id"] = line[4:]
        payload.append(ev)
    return payload


class ProcessGateway:
    def __init__(self, port, log):
        self.port = port
        self.log = log
        self.proc = None
        self.capture = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def launch(self, capture):
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("old gateway still running")
        self.capture = capture
        cmd = [
            sys.executable,
            "-u",
            str(ROOT / "backend/v35_unified_gateway.py"),
            "--archive",
            str(capture),
            "--manifest-sha256",
            pin(capture),
            "--port",
            str(self.port),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "backend")
        self.proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        last = ""
        while time.monotonic() - started < 20:
            if self.proc.poll() is not None:
                raise RuntimeError(f"gateway crashed {self.proc.returncode}")
            try:
                status, raw, _ = endpoint(self.base, "/api/v35/health", timeout=2)
                if status == 200:
                    h = json.loads(raw)
                    if h["source_manifest_sha256"] != pin(capture):
                        raise RuntimeError("Gateway wrong source")
                    return {
                        "launch_seconds": round(time.monotonic() - started, 4),
                        "pid": self.proc.pid,
                        "digest": h["final_state_sha256"],
                        "manifest": h["source_manifest_sha256"],
                    }
            except (URLError, ConnectionError, OSError) as exc:
                last = str(exc)
            time.sleep(0.07)
        raise TimeoutError("gateway not ready " + last)

    def terminate(self, force=False):
        if not self.proc:
            return
        p = self.proc
        if p.poll() is None:
            if force:
                p.kill()
            else:
                p.terminate()
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        self.proc = None
        return p.returncode


def soak(seconds, clients, report_path):
    result = {
        "schema": "v35_3_real_coinbase_sse_process_soak_v1",
        "status": "RUNNING",
        "requested_duration_seconds": seconds,
        "simultaneous_clients": clients,
        "market_source": "AUTHENTIC_FROZEN_COINBASE_ARCHIVE",
        "live_feed": False,
        "trading_enabled": False,
        "real_orders": 0,
        "browser_dom_test": False,
        "cursor_contract": "manifest_sha256:product:state_version",
        "restart_events": [],
        "observations": [],
        "fault_tests": {},
        "worker_errors": [],
    }
    original = pin(CLEAN)
    secondary = pin(SECOND)
    ds = replay_capture(CLEAN, original)
    ds2 = replay_capture(SECOND, secondary)
    result["source_manifest_sha256"] = original
    result["alternate_manifest_sha256"] = secondary
    result["source_message_count"] = ds.message_count
    result["source_state_digest"] = ds.final_state_sha256
    row_map = {
        product: {r["state_version"]: r for r in ds.rows[product]}
        for product in ("BTC-USD", "ETH-USD")
    }
    log = open(report_path.with_suffix(".gateway.log"), "w", encoding="utf8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ProcessGateway(port, log)
    pause = Event()
    stop = Event()
    lock = Lock()
    latencies = []
    successes = Counter()
    exceptions = Counter()
    sampled = []
    starts = []
    max_rss = 0
    max_fd = 0

    def one_client(index):
        product = ("BTC-USD", "ETH-USD")[index % 2]
        rows = ds.rows[product]
        pin_ = original
        while not stop.is_set():
            if pause.is_set():
                time.sleep(0.03)
                continue
            try:
                raw_pages = []
                for k in (0, 1):
                    headers = (
                        {} if k == 0 else {"Last-Event-ID": raw_pages[0][-2]["id"]}
                    )
                    status, raw, lat = endpoint(
                        server.base,
                        "/api/v35/stream?product=" + product + "&limit=12",
                        headers,
                        timeout=5,
                    )
                    if status != 200:
                        raise AssertionError(f"HTTP {status} on phase {k}")
                    page = events(raw)
                    if (
                        not page
                        or page[0].get("name") != "source"
                        or page[-1].get("name") != "replay_end"
                    ):
                        raise AssertionError("missing source or replay_end")
                    if page[0]["data"]["source_manifest_sha256"] != pin_:
                        raise AssertionError("SOURCE_MISMATCH")
                    state = [e for e in page if e.get("name") == "state"]
                    if len(state) != 12:
                        raise AssertionError("missing/duplicate events")
                    for j, e in enumerate(state):
                        expected = rows[k * 12 + j]
                        if e["data"] != expected:
                            raise AssertionError("STATE_BYTES_OR_PROVENANCE_MISMATCH")
                        token = f"{pin_}:{product}:{expected['state_version']}"
                        if e.get("id") != token:
                            raise AssertionError("INVALID_CURSOR_ID")
                    expected_cursor = rows[k * 12 + 11]["state_version"]
                    if page[-1]["data"]["next_after"] != expected_cursor:
                        raise AssertionError("END_CURSOR_MISMATCH")
                    raw_pages.append(page)
                    with lock:
                        latencies.append(lat)
                        successes[product] += 1
            except (
                URLError,
                ConnectionError,
                TimeoutError,
                OSError,
                AssertionError,
            ) as e:
                # There is a deliberate crash/restart window. Mark all failures,
                # and distinguish them from source/content corruption.
                with lock:
                    exceptions[type(e).__name__ + ":" + str(e)[:90]] += 1
                time.sleep(0.07)
            time.sleep(0.12)

    try:
        first = server.launch(CLEAN)
        starts.append(first)
        # Verify no fabricated new events after a finite archive's actual EOF.
        last = ds.rows["BTC-USD"][-1]["state_version"]
        source_cursor = f"{original}:BTC-USD:{last}"
        status, raw, _ = endpoint(
            server.base,
            "/api/v35/stream?product=BTC-USD&limit=3",
            {"Last-Event-ID": source_cursor},
        )
        eof = events(raw)
        assert status == 200 and not any(e["name"] == "state" for e in eof)
        assert eof[-1]["data"]["has_more"] is False
        result["fault_tests"]["finite_eof_no_invented_ticks"] = "PASS"
        # Hold all header-processing worker slots to demonstrate bounded refusal.
        sockets = []
        try:
            for _ in range(10):
                sock = socket.create_connection(("127.0.0.1", port), timeout=1.5)
                sock.sendall(
                    b"GET /api/v35/health HTTP/1.1\r\nHost: localhost\r\nX-Pending: "
                )
                sockets.append(sock)
            time.sleep(0.25)
        finally:
            for sock in sockets:
                sock.close()
        time.sleep(0.3)
        r, _, _ = endpoint(server.base, "/api/v35/health")
        assert r == 200, "Failed to restore after header-flood"
        result["fault_tests"]["header_flood_recovery"] = "PASS"
        # Six worker clients issue 2 sequential SSE windows each iteration.
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=clients) as executor:
            futures = [executor.submit(one_client, i) for i in range(clients)]
            done_restart = False
            done_switch = False
            done_silence = False
            while time.monotonic() - started < seconds:
                elapsed = time.monotonic() - started
                if not done_restart and elapsed >= seconds * 0.35:
                    pause.set()
                    time.sleep(0.65)
                    before = time.monotonic()
                    forced_code = server.terminate(force=True)
                    next_launch = server.launch(CLEAN)
                    assert next_launch["digest"] == ds.final_state_sha256
                    status, _, _ = endpoint(
                        server.base,
                        "/api/v35/stream?product=BTC-USD&limit=1",
                        {"Last-Event-ID": source_cursor},
                    )
                    assert status == 200, (
                        "Cursor changed across restart of identical source"
                    )
                    result["restart_events"].append(
                        {
                            "kind": "SIGKILL_same_archive",
                            "old_exit": forced_code,
                            "recovery_seconds": round(time.monotonic() - before, 3),
                            "digest_preserved": True,
                            "cursor_resumed": True,
                        }
                    )
                    starts.append(next_launch)
                    done_restart = True
                    pause.clear()
                if not done_switch and elapsed >= seconds * 0.65:
                    pause.set()
                    time.sleep(0.7)
                    before = time.monotonic()
                    server.terminate()
                    switched = server.launch(SECOND)
                    status, raw, _ = endpoint(
                        server.base,
                        "/api/v35/stream?product=BTC-USD&limit=2",
                        {"Last-Event-ID": source_cursor},
                    )
                    assert (
                        status == 409
                        and json.loads(raw)["error"] == "SOURCE_CHANGED_RESYNC_REQUIRED"
                    )
                    server.terminate()
                    restored = server.launch(CLEAN)
                    assert restored["digest"] == ds.final_state_sha256
                    result["restart_events"].append(
                        {
                            "kind": "DIFFERENT_REAL_COINBASE_ARCHIVE",
                            "recovery_seconds": round(time.monotonic() - before, 3),
                            "cross_source_resume_status": status,
                            "cross_source_rejected": True,
                            "original_restored": True,
                        }
                    )
                    starts.extend([switched, restored])
                    done_switch = True
                    pause.clear()
                if not done_silence and elapsed >= seconds * 0.80:
                    pause.set()
                    time.sleep(0.7)
                    before = time.monotonic()
                    initial = json.loads(
                        endpoint(server.base, "/api/v35/snapshot?product=BTC-USD")[1]
                    )["state"]
                    time.sleep(4.0)  # Longer than 3-second socket timeout.
                    later = json.loads(
                        endpoint(server.base, "/api/v35/snapshot?product=BTC-USD")[1]
                    )["state"]
                    assert later == initial, (
                        "Invented price/state during finite replay silence"
                    )
                    result["fault_tests"]["finite_archive_silence_no_new_price"] = (
                        "PASS"
                    )
                    result["fault_tests"]["source_silence_observation_seconds"] = round(
                        time.monotonic() - before, 3
                    )
                    done_silence = True
                    pause.clear()
                try:
                    proc = psutil.Process(server.proc.pid)
                    rss = proc.memory_info().rss
                    fds = proc.num_fds()
                    cpu = proc.cpu_times()
                    status, raw, _ = endpoint(
                        server.base, "/api/v35/metrics", timeout=2
                    )
                    if status == 200:
                        m = json.loads(raw)
                        max_rss = max(max_rss, rss)
                        max_fd = max(max_fd, fds)
                        sampled.append(
                            {
                                "t_s": round(elapsed, 2),
                                "pid": server.proc.pid,
                                "rss_bytes": rss,
                                "file_descriptors": fds,
                                "cpu_seconds": round(cpu.user + cpu.system, 4),
                                "active_connections": m["active_connections"],
                                "peak_connections": m["peak_connections"],
                                "sse_started": m["sse_started"],
                                "sse_completed": m["sse_completed"],
                                "sse_aborted": m["sse_aborted"],
                                "rejected_client_slots": m["rejected_client_slots"],
                                "resync_required": m["resync_required"],
                                "sse_queue_depth": m["sse_queue_depth"],
                            }
                        )
                except (OSError, URLError, psutil.Error):
                    pass
                time.sleep(0.35)
            stop.set()
            pause.clear()
            for f in futures:
                f.result(timeout=8)
        final_code, raw, _ = endpoint(server.base, "/api/v35/health")
        assert final_code == 200
        health = json.loads(raw)
        assert health["final_state_sha256"] == ds.final_state_sha256
        assert health["source_manifest_sha256"] == original
        assert all(s["sse_queue_depth"] == 0 for s in sampled)
        assert all(s["peak_connections"] <= 8 for s in sampled)
        assert all(s["active_connections"] >= 0 for s in sampled)
        assert successes["BTC-USD"] > 0 and successes["ETH-USD"] > 0
        result["status"] = "PASS"
    except Exception as exc:
        result["status"] = "FAIL"
        result["fatal_error"] = repr(exc)
    finally:
        stop.set()
        pause.clear()
        server.terminate()
        log.close()
        result["elapsed_seconds"] = (
            round(time.monotonic() - started, 3) if "started" in locals() else 0
        )
        result["successful_sse_responses"] = dict(successes)
        result["successful_sse_responses_total"] = sum(successes.values())
        result["worker_exceptions"] = dict(exceptions)
        result["request_latency_ms"] = {
            "samples": len(latencies),
            "p50": round(statistics.median(latencies), 2) if latencies else None,
            "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2)
            if latencies
            else None,
            "p99": round(sorted(latencies)[int(0.99 * (len(latencies) - 1))], 2)
            if latencies
            else None,
            "max": round(max(latencies), 2) if latencies else None,
        }
        result["samples"] = len(sampled)
        result["peak_rss_bytes"] = max_rss
        result["peak_fds"] = max_fd
        result["launches"] = starts
        result["sampled_metrics"] = sampled
        if sampled:
            from collections import defaultdict

            by_pid = defaultdict(list)
            for s in sampled:
                by_pid[s["pid"]].append(s)
            result["per_process_rss"] = [
                {
                    "pid": pid,
                    "samples": len(rows),
                    "first_rss": rows[0]["rss_bytes"],
                    "last_rss": rows[-1]["rss_bytes"],
                    "delta_rss_bytes": rows[-1]["rss_bytes"] - rows[0]["rss_bytes"],
                    "peak_rss": max(x["rss_bytes"] for x in rows),
                }
                for pid, rows in by_pid.items()
            ]
        report_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf8"
        )
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--duration-seconds", type=int, default=120)
    p.add_argument("--clients", type=int, default=6)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    if (
        args.duration_seconds < 30
        or args.duration_seconds > 3600
        or args.clients < 1
        or args.clients > 8
    ):
        p.error("use 30..3600 seconds and 1..8 clients")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result = soak(args.duration_seconds, args.clients, args.report)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "sampled_metrics"}, indent=2
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
