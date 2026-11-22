"""Fail-fast Q22 5-hour Coinbase public-source CI supervisor.

Does NOT change the 24h production observer. One private source connection is
observed for five hours; 24 separate eight-client four-minute LIVE SSE windows
sample the run. Bad health, source gap, RSS, disk or a failed SSE window cancels
the source promptly. No raw data/credentials are put in CI artifacts.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from q21_host_witness import unix_json  # noqa: E402
from q22_ci_long_observer import (  # noqa: E402
    ARCHIVE_BUDGET,
    CI_DURATION,
    DISK_RESERVE,
    RAW_BUDGET,
    SMOKE_DURATION,
)

MONITOR_INTERVAL = 15
MEMORY_CEILING = 2 * 1024**3
FD_CEILING = 512
MIN_DISK_MARGIN = 500_000_000
Q22_WINDOWS = 24
Q22_WINDOW_SECONDS = 240
Q22_WINDOW_PERIOD = 720
Q22_FIRST_WINDOW = 330
Q22_SMOKE_FIRST_WINDOW = 45


class Q22Rejected(ValueError):
    pass


def check_private_health(health: dict, metrics: dict, previous: dict | None) -> dict:
    now_ms = time.time_ns() // 1_000_000
    if (
        health.get("mode") != "LIVE_SOURCE"
        or health.get("status") != "HEALTHY"
        or health.get("live_feed") is not True
        or health.get("operationally_healthy") is not True
        or health.get("trading_enabled") is not False
        or set(health.get("snapshots", [])) != {"BTC-USD", "ETH-USD"}
        or set(health.get("subscriptions", []))
        != {"level2", "market_trades", "heartbeats"}
        or health.get("connection_gaps") != 0
        or health.get("connection_id") != 0
        or metrics.get("max_sse_clients") != 8
        or metrics.get("monitor_transport") != "UNIX_SOCKET"
        or metrics.get("trading_enabled") is not False
        or metrics.get("execution_authorized") is not False
        or not isinstance(health.get("session_id"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", health["session_id"])
        or any(type(health.get(k)) is not int or health[k] < 0
               for k in ("messages", "last_sequence", "heartbeats",
                         "last_receiver_time_ms"))
        or health["messages"] <= 0
        or health["last_sequence"] != health["messages"] - 1
        or not 0 <= now_ms - health["last_receiver_time_ms"] <= 15000
        or type(metrics.get("active_sse_connections")) is not int
        or not 0 <= metrics["active_sse_connections"] <= 8
    ):
        raise Q22Rejected("PRIVATE_SOURCE_OR_TRANSPORT_UNHEALTHY")
    session_hash = sha256(health["session_id"].encode()).hexdigest()
    if previous and (
        session_hash != previous["session_hash"]
        or health["messages"] <= previous["messages"]
        or health["heartbeats"] < previous["heartbeats"]
    ):
        raise Q22Rejected("SOURCE_SESSION_CHANGED_OR_NO_MESSAGE_PROGRESS")
    return {
        "session_hash": session_hash,
        "messages": health["messages"],
        "heartbeats": health["heartbeats"],
        "staleness_ms": now_ms - health["last_receiver_time_ms"],
        "active_sse": metrics["active_sse_connections"],
    }


def process_stat(pid: int) -> dict:
    if type(pid) is not int or pid <= 1:
        raise Q22Rejected("INVALID_PROCESS_PID")
    folder = Path("/proc") / str(pid)
    status = (folder / "status").read_text()
    rss = next((int(row.split()[1]) * 1024 for row in status.splitlines()
                if row.startswith("VmRSS:")), None)
    if rss is None:
        raise Q22Rejected("MISSING_LINUX_PROCESS_RSS")
    text = (folder / "stat").read_text()
    after_name = text.rsplit(")", 1)[1].split()
    return {
        "pid": pid,
        "rss": rss,
        "fds": len(list((folder / "fd").iterdir())),
        "cpu_ticks": int(after_name[11]) + int(after_name[12]),
    }


def child_process(observer_pid: int) -> int:
    childfile = (Path("/proc") / str(observer_pid) / "task"
                 / str(observer_pid) / "children")
    children = childfile.read_text().split()
    if len(children) != 1:
        raise Q22Rejected("OBSERVED_NODE_COLLECTOR_PID_NOT_UNIQUE")
    return int(children[0])


def private_archive_size(work: Path) -> int:
    source_root = work / "soak_evidence"
    if not source_root.is_dir() or source_root.is_symlink():
        raise Q22Rejected("RAW_CAPTURE_SOURCE_DIRECTORY_MISSING")
    sources = list(source_root.glob("coinbase_btc_eth_soak_*"))
    if len(sources) != 1:
        raise Q22Rejected("AMBIGUOUS_REAL_CAPTURE_SOURCE")
    chunks = sources[0] / "chunks"
    if not chunks.is_dir() or chunks.is_symlink():
        raise Q22Rejected("UNSAFE_CAPTURE_CHUNK_DIRECTORY")
    size = 0
    for child in chunks.iterdir():
        if child.is_symlink() or not child.is_file() or not (
            child.name.endswith(".jsonl.partial")
            or child.name.endswith(".jsonl")
        ):
            raise Q22Rejected("UNEXPECTED_OR_SYMLINKED_SOURCE_CHUNK")
        size += child.stat().st_size
    return size


def projected_archive_bound(elapsed: float, observed_bytes: int,
                            disk_free: int, duration: int) -> dict:
    if (
        not 30 <= elapsed <= duration
        or type(observed_bytes) is not int
        or observed_bytes < 100_000
        or type(disk_free) is not int
    ):
        raise Q22Rejected("INVALID_EARLY_SOURCE_RATE_OR_DISK_SAMPLE")
    projected = int(observed_bytes * duration / elapsed * 2.5)
    if (
        projected > min(int(0.85 * ARCHIVE_BUDGET), RAW_BUDGET)
        or disk_free - max(0, projected - observed_bytes)
        < DISK_RESERVE + MIN_DISK_MARGIN
    ):
        raise Q22Rejected("FIVE_HOUR_RATE_OR_DISK_PROJECTION_UNSAFE")
    return {
        "observed_bytes": observed_bytes,
        "projected_archived_bytes_2p5x": projected,
    }


def evaluate_window(output: str, seconds: int) -> dict:
    match = re.search(r"PASS_REAL_PUBLIC_LIVE_FANOUT (\{[^\n]+\})", output)
    if match is None or len(match.group(1)) > 100_000:
        raise Q22Rejected("REAL_EIGHT_CLIENT_FANOUT_FAILED_OR_NO_REPORT")
    report = json.loads(match.group(1))
    clients = report.get("clients")
    if (
        report.get("schema") != "v36_3_genuine_public_live_fanout_v1"
        or report.get("status") != "PASS"
        or report.get("mode") != "REAL_PUBLIC_COINBASE_LIVE_NOT_ARCHIVE"
        or report.get("trading_enabled") is not False
        or report.get("certified_24h") is not False
        or not isinstance(clients, list)
        or len(clients) != 8
        or report.get("max_active_sse", 0) < 8
        or report.get("elapsed_seconds", 0) < seconds - 5
    ):
        raise Q22Rejected("SSE_WINDOW_CONTRACT_FAILED")
    for i, client in enumerate(clients):
        if (
            client.get("client") != i
            or client.get("product") != ("BTC-USD", "ETH-USD")[i % 2]
            or client.get("snapshots") != 1
            or client.get("errors")
            or (i != 7 and client.get("states", 0) < 20)
            or (i != 7 and client.get("resync", 0) != 0)
        ):
            raise Q22Rejected("SSE_INDIVIDUAL_CLIENT_FAILED:" + str(i))
    return {
        "status": "PASS",
        "max_active_sse": report["max_active_sse"],
        "elapsed_seconds": report["elapsed_seconds"],
        "total_states": sum(v["states"] for v in clients),
        "health_latency_p95_ms": report["health_latency_p95_ms"],
        "slow_client_resync": clients[7]["resync"],
    }


def record_sample(work: Path, observer_pid: int, known_child: int | None,
                  previous: dict | None, index: int) -> tuple[dict, int]:
    socket_path = work / "v36_monitor.sock"
    health = unix_json(socket_path, "/api/v36/health")
    metrics = unix_json(socket_path, "/api/v36/metrics")
    source = check_private_health(health, metrics, previous)
    child = child_process(observer_pid)
    if known_child is not None and child != known_child:
        raise Q22Rejected("COLLECTOR_RESTART_NOT_CONTINUOUS")
    proc = [process_stat(p) for p in (observer_pid, child)]
    rss_total = sum(p["rss"] for p in proc)
    fds_total = sum(p["fds"] for p in proc)
    v = os.statvfs(work)
    free = v.f_bavail * v.f_frsize
    if (
        rss_total > MEMORY_CEILING
        or fds_total > FD_CEILING
        or free < DISK_RESERVE + MIN_DISK_MARGIN
    ):
        raise Q22Rejected("RESOURCE_LIMIT_EXCEEDED")
    return {
        "index": index,
        "monotonic_ns": time.monotonic_ns(),
        "source": source,
        "observer_pid": observer_pid,
        "collector_pid": child,
        "rss_total_bytes": rss_total,
        "fd_total": fds_total,
        "cpu_ticks_total": sum(p["cpu_ticks"] for p in proc),
        "disk_free_bytes": free,
        "trading_enabled": False,
    }, child


def evaluate_monitoring(samples: list[dict], seconds: int,
                        windows: list[dict], expected_windows: int) -> dict:
    min_samples = 900 if seconds == CI_DURATION else 18
    if len(samples) < min_samples or len(windows) != expected_windows:
        raise Q22Rejected("INSUFFICIENT_HOST_MONITORING_OR_SSE_COVERAGE")
    first = samples[0]
    for i, s in enumerate(samples):
        if (
            s["index"] != i
            or s["source"]["session_hash"] != first["source"]["session_hash"]
            or s["observer_pid"] != first["observer_pid"]
            or s["collector_pid"] != first["collector_pid"]
            or s["trading_enabled"] is not False
            or s["disk_free_bytes"] < DISK_RESERVE + MIN_DISK_MARGIN
            or s["rss_total_bytes"] > MEMORY_CEILING
        ):
            raise Q22Rejected("SOURCE_SESSION_OR_HOST_RESOURCE_WITNESS_CHANGED")
        if i:
            diff = s["monotonic_ns"] - samples[i - 1]["monotonic_ns"]
            if not 0 < diff <= 40_000_000_000:
                raise Q22Rejected("HOST_MONITOR_SAMPLE_GAP")
    return {
        "sample_count": len(samples),
        "sample_span_seconds": round(
            (samples[-1]["monotonic_ns"] - first["monotonic_ns"]) / 1e9, 3),
        "max_rss_bytes": max(x["rss_total_bytes"] for x in samples),
        "max_open_fds": max(x["fd_total"] for x in samples),
        "min_disk_free_bytes": min(x["disk_free_bytes"] for x in samples),
        "total_source_messages_last_sample": samples[-1]["source"]["messages"],
        "max_source_age_ms": max(x["source"]["staleness_ms"] for x in samples),
        "source_session_stable": True,
        "collector_pid_stable": True,
        "windows_passed": len(windows),
        "sse_total_states": sum(x["total_states"] for x in windows),
        "real_orders_enabled": False,
    }


def _shutdown(observer: subprocess.Popen, collector: int | None):
    if observer.poll() is None:
        observer.send_signal(signal.SIGINT)
        try:
            observer.wait(timeout=14)
        except subprocess.TimeoutExpired:
            observer.kill()
            observer.wait(timeout=4)
    if collector and (Path("/proc") / str(collector)).exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(collector, signal.SIGTERM)


def run(work: Path, public: Path, seconds: int, port: int) -> dict:
    if seconds not in (CI_DURATION, SMOKE_DURATION):
        raise Q22Rejected("UNSUPPORTED_CI_DURATION")
    if work.exists() or work.is_symlink():
        raise Q22Rejected("CI_CAPTURE_WORKDIR_MUST_BE_NEW")
    if not public.is_dir() or public.is_symlink():
        raise Q22Rejected("CI_PUBLIC_SUMMARY_DIRECTORY_MISSING")
    smoke = seconds == SMOKE_DURATION
    window_count = 1 if smoke else Q22_WINDOWS
    window_duration = 60 if smoke else Q22_WINDOW_SECONDS
    first_window = Q22_SMOKE_FIRST_WINDOW if smoke else Q22_FIRST_WINDOW
    period = 720 if not smoke else 120
    preflight_time = 40 if smoke else 300
    interval = 5 if smoke else MONITOR_INTERVAL
    child = None
    samples = []
    windows = []
    started = time.monotonic()
    prior = None
    next_sample = started
    window_active = None
    window_started = None
    window_log = None
    drain_started = None
    preflight = None
    was_ready = False
    # Private logs live OUTSIDE the public summary artifact directory.
    with (
        Path(str(work) + ".observer_stdout.log").open("x") as out,
        Path(str(work) + ".observer_stderr.log").open("x") as err,
    ):
        observer = subprocess.Popen(
            [
                sys.executable, "-u",
                str(ROOT / "scripts/q22_ci_long_observer.py"),
                "--execute-public-capture", "--duration", str(seconds),
                "--workdir", str(work), "--port", str(port),
            ],
            stdout=out, stderr=err, start_new_session=True,
        )
        try:
            while True:
                now = time.monotonic()
                elapsed = now - started
                rc = observer.poll()
                if rc is not None:
                    if rc != 0 or elapsed < seconds - 5:
                        raise Q22Rejected("COLLECTOR_EXITED_EARLY_OR_FAILED")
                    break
                if elapsed > seconds + 50:
                    raise Q22Rejected("COLLECTOR_EXCEEDED_CAPTURE_DEADLINE")
                if not was_ready:
                    try:
                        sample, child = record_sample(
                            work, observer.pid, child, None, len(samples))
                        was_ready = True
                        samples.append(sample)
                        prior = sample["source"]
                        next_sample = now + interval
                    except (OSError, ValueError, KeyError) as exc:
                        if elapsed > 90:
                            raise Q22Rejected("LIVE_COLLECTOR_NEVER_HEALTHY") from exc
                        time.sleep(1)
                        continue
                if now >= next_sample:
                    s, child = record_sample(
                        work, observer.pid, child, prior, len(samples))
                    if samples and not 0 < s["monotonic_ns"] - samples[-1]["monotonic_ns"] <= 40_000_000_000:
                        raise Q22Rejected("MONITOR_WALLCLOCK_OR_EVENT_LOOP_GAP")
                    samples.append(s)
                    prior = s["source"]
                    next_sample = now + interval
                if preflight is None and elapsed >= preflight_time:
                    archived = private_archive_size(work)
                    preflight = projected_archive_bound(
                        elapsed, archived,
                        samples[-1]["disk_free_bytes"], seconds,
                    )
                    (public / "preflight.json").write_text(
                        json.dumps({"schema": "q22_live_five_hour_preflight_v1",
                                    "source_mode": "LIVE_SOURCE",
                                    "preflight_seconds": round(elapsed, 2),
                                    **preflight, "trading_enabled": False},
                                   sort_keys=True, indent=2) + "\n")
                    print("PASS_Q22_EARLY_RUN_RATE_DISK_PREFLIGHT", flush=True)
                if window_active is not None and window_active.poll() is not None:
                    rc = window_active.wait()
                    window_log.close()
                    output = Path(window_log.name).read_text()
                    if rc != 0:
                        raise Q22Rejected("REAL_SSE_WINDOW_PROCESS_FAILED")
                    summary = evaluate_window(output, window_duration)
                    summary["window"] = len(windows) + 1
                    windows.append(summary)
                    window_active = None
                    drain_started = now
                if drain_started is not None:
                    m = unix_json(work / "v36_monitor.sock", "/api/v36/metrics")
                    if (
                        m.get("active_sse_connections") == 0
                        and m.get("operationally_healthy") is True
                    ):
                        drain_started = None
                    elif now - drain_started >= 25:
                        raise Q22Rejected("SSE_SLOTS_NOT_DRAINED_BEFORE_NEXT_WINDOW")
                next_idx = len(windows)
                if (
                    preflight is not None
                    and window_active is None
                    and drain_started is None
                    and next_idx < window_count
                    and elapsed >= first_window + next_idx * period
                ):
                    log_path = work / ("q22_fanout_%02d.log" % (next_idx + 1))
                    window_log = log_path.open("x")
                    window_active = subprocess.Popen(
                        [sys.executable, "-u", str(
                            ROOT / "scripts/v36_3_real_live_fanout.py"),
                         "--seconds", str(window_duration),
                         "--port", str(port),
                         "--observer-pid", str(observer.pid)],
                        stdout=window_log, stderr=subprocess.STDOUT,
                    )
                    window_started = now
                if (
                    window_active is not None
                    and now - window_started > window_duration + 28
                ):
                    raise Q22Rejected("REAL_SSE_WINDOW_TIME_BUDGET_EXCEEDED")
                time.sleep(1)
            if window_active is not None or drain_started is not None:
                raise Q22Rejected("SSE_WINDOW_NOT_FINISHED_AT_CAPTURE_END")
            result = evaluate_monitoring(samples, seconds, windows, window_count)
            if seconds == CI_DURATION and result["sample_span_seconds"] < 17400:
                raise Q22Rejected("HOST_SAMPLE_SPAN_SHORTER_THAN_FIVE_HOUR_SOURCE")
            result.update({
                "schema": "q22_live_ci_source_sse_resource_v1",
                "status": "PASS",
                "capture_target_seconds": seconds,
                "sse_windows": windows,
                "preflight": preflight,
                "ephemeral_runner_only": True,
                "24h_persistent_host_certified": False,
                "order_execution_enabled": False,
            })
            (public / "live_monitoring_summary.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n")
            print("PASS_Q22_SOURCE_FANOUT_RESOURCE_MONITORING "
                  + json.dumps({k: v for k, v in result.items()
                                if k != "sse_windows"}, sort_keys=True), flush=True)
            return result
        except (Q22Rejected, OSError, ValueError, KeyError) as exc:
            (public / "fail_fast.json").write_text(json.dumps({
                "schema": "q22_fail_fast_v1",
                "reason": type(exc).__name__ + ":" + str(exc).splitlines()[0][:180],
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "sse_windows_passed": len(windows),
                "market_source_unverified": True,
                "real_orders_enabled": False,
            }, sort_keys=True, indent=2) + "\n")
            raise
        finally:
            if window_active is not None:
                if window_active.poll() is None:
                    window_active.terminate()
                    try:
                        window_active.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        window_active.kill()
                        window_active.wait(timeout=5)
                if window_log is not None and not window_log.closed:
                    window_log.close()
            _shutdown(observer, child)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private-workdir", type=Path, required=True)
    p.add_argument("--public-summary", type=Path, required=True)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--duration", type=int, choices=[SMOKE_DURATION, CI_DURATION],
                   default=CI_DURATION)
    p.add_argument("--execute-public-capture", action="store_true", required=True)
    args = p.parse_args(argv)
    try:
        run(args.private_workdir, args.public_summary, args.duration, args.port)
        return 0
    except (Q22Rejected, ValueError, OSError, KeyError, TimeoutError) as exc:
        print("FAIL_Q22_FAIL_FAST_SUPERVISOR " + type(exc).__name__ + ":"
              + str(exc).splitlines()[0][:180], flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
