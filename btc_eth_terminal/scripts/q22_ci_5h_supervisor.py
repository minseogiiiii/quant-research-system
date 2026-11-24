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
from q23_redacted_diagnostics import (  # noqa: E402
    failure_receipt,
    named_failures,
    read_private_status,
)
from q24_ws_diagnostic_receipt import publish_receipt  # noqa: E402
from q27_heartbeat_receipt import publish_receipt as publish_q27_receipt  # noqa: E402
from q28_transport_incident_receipt import publish_incident  # noqa: E402

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


def check_private_health(
    health: dict, metrics: dict, previous: dict | None,
    health_http: int = 200, metrics_http: int = 200,
) -> dict:
    now_ms = time.time_ns() // 1_000_000
    codes = named_failures(
        health, metrics, previous, now_ms=now_ms,
        health_http=health_http, metrics_http=metrics_http,
    )
    if codes:
        raise Q22Rejected(
            "PRIVATE_SOURCE_OR_TRANSPORT_UNHEALTHY:" + ",".join(codes)
        )
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
    health_http, health = read_private_status(socket_path, "/api/v36/health")
    metrics_http, metrics = read_private_status(socket_path, "/api/v36/metrics")
    source = check_private_health(
        health, metrics, previous, health_http, metrics_http
    )
    # Q25 CI-only enforcement. The live market gate remains strictly
    # single-connection and zero-gap; the new guard does not authorize
    # resumed sessions or change any release/trading policy.
    q25_enabled = os.environ.get("Q25_SESSION_GUARD_ENABLED") == "1"
    if q25_enabled and (
        health.get("q25_isolation_schema") != "q25_source_epoch_isolation_v1"
        or type(health.get("q25_revocations_total")) is not int
        or health["q25_revocations_total"] != 0
        or health.get("q25_orders_enabled") is not False
    ):
        raise Q22Rejected("Q25_SOURCE_EPOCH_GUARD_NOT_VERIFIED")
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
        "q25_epoch_guard_verified": q25_enabled,
    }, child


def evaluate_monitoring(samples: list[dict], seconds: int,
                        windows: list[dict], expected_windows: int) -> dict:
    min_samples = 900 if seconds == CI_DURATION else 18
    if len(samples) < min_samples or len(windows) != expected_windows:
        raise Q22Rejected("INSUFFICIENT_HOST_MONITORING_OR_SSE_COVERAGE")
    first = samples[0]
    cpu_hz = os.sysconf("SC_CLK_TCK")
    available_cpus = len(os.sched_getaffinity(0))
    cpu_cores = []
    cpu_saturated_samples = 0
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
            previous = samples[i - 1]
            diff = s["monotonic_ns"] - previous["monotonic_ns"]
            ticks = s["cpu_ticks_total"] - previous["cpu_ticks_total"]
            if not 0 < diff <= 40_000_000_000 or ticks < 0:
                raise Q22Rejected("HOST_MONITOR_SAMPLE_GAP_OR_CPU_REGRESSION")
            cores = ticks / (diff / 1e9 * cpu_hz)
            cpu_cores.append(cores)
            if cores > available_cpus * 1.25:
                raise Q22Rejected("SUSTAINED_CPU_COUNTER_OR_HOST_LIMIT_INVALID")
            cpu_saturated_samples = (
                cpu_saturated_samples + 1
                if cores >= 0.95 * available_cpus else 0
            )
            if cpu_saturated_samples >= 8:
                raise Q22Rejected("TWO_MINUTES_OF_HOST_CPU_SATURATION")
    rss_baseline = max(
        x["rss_total_bytes"] for x in samples[: min(12, len(samples))]
    )
    rss_growth = max(0, samples[-1]["rss_total_bytes"] - rss_baseline)
    if rss_growth > 1_100_000_000:
        raise Q22Rejected("SUSPICIOUS_OBSERVER_COLLECTOR_RSS_GROWTH")
    return {
        "sample_count": len(samples),
        "sample_span_seconds": round(
            (samples[-1]["monotonic_ns"] - first["monotonic_ns"]) / 1e9, 3),
        "max_rss_bytes": max(x["rss_total_bytes"] for x in samples),
        "rss_growth_bytes": rss_growth,
        "cpu_cores_peak": round(max(cpu_cores, default=0), 3),
        "cpu_cores_p95": round(
            sorted(cpu_cores)[int((len(cpu_cores) - 1) * 0.95)]
            if cpu_cores else 0, 3),
        "available_cpus": available_cpus,
        "max_open_fds": max(x["fd_total"] for x in samples),
        "min_disk_free_bytes": min(x["disk_free_bytes"] for x in samples),
        "total_source_messages_last_sample": samples[-1]["source"]["messages"],
        "max_source_age_ms": max(x["source"]["staleness_ms"] for x in samples),
        "source_session_stable": True,
        "collector_pid_stable": True,
        "q25_epoch_guard_verified": all(
            x.get("q25_epoch_guard_verified") is True for x in samples
        ),
        "q27_30m_guarded_only": (
            os.environ.get("Q27_30M_SOAK_ENABLED") == "1"
            and os.environ.get("Q27_HEARTBEAT_GUARD_ENABLED") == "1"
        ),
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
    guarded_30m = (
        seconds == 1800
        and os.environ.get("Q27_30M_SOAK_ENABLED") == "1"
        and os.environ.get("Q27_HEARTBEAT_GUARD_ENABLED") == "1"
        and os.environ.get("Q24_WS_DIAG_ENABLED") == "1"
        and os.environ.get("Q25_SESSION_GUARD_ENABLED") == "1"
    )
    if seconds not in (CI_DURATION, SMOKE_DURATION) and not guarded_30m:
        raise Q22Rejected("UNSUPPORTED_CI_DURATION")
    if work.exists() or work.is_symlink():
        raise Q22Rejected("CI_CAPTURE_WORKDIR_MUST_BE_NEW")
    if not public.is_dir() or public.is_symlink():
        raise Q22Rejected("CI_PUBLIC_SUMMARY_DIRECTORY_MISSING")
    smoke = seconds == SMOKE_DURATION
    window_count = 1 if smoke else 3 if guarded_30m else Q22_WINDOWS
    window_duration = 60 if smoke else 120 if guarded_30m else Q22_WINDOW_SECONDS
    first_window = (
        Q22_SMOKE_FIRST_WINDOW if smoke
        else 310 if guarded_30m else Q22_FIRST_WINDOW
    )
    period = 120 if smoke else 500 if guarded_30m else 720
    preflight_time = 40 if smoke else 180 if guarded_30m else 300
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
    last_progress_slot = 0
    failfast = False
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
                # The final source-stop transition is adjudicated by the
                # completed collector manifest, not as a transient LIVE fault.
                if now >= next_sample and elapsed < seconds - 8:
                    s, child = record_sample(
                        work, observer.pid, child, prior, len(samples))
                    if samples and not (
                        0 < s["monotonic_ns"] - samples[-1]["monotonic_ns"]
                        <= 40_000_000_000
                    ):
                        raise Q22Rejected("MONITOR_WALLCLOCK_OR_EVENT_LOOP_GAP")
                    samples.append(s)
                    prior = s["source"]
                    next_sample = now + interval
                progress_slot = int(elapsed // 300)
                if not smoke and progress_slot > last_progress_slot and samples:
                    last_progress_slot = progress_slot
                    print("Q22_5H_PROGRESS " + json.dumps({
                        "elapsed_minutes": round(elapsed / 60, 2),
                        "messages": samples[-1]["source"]["messages"],
                        "window_passed": len(windows),
                        "rss_bytes": samples[-1]["rss_total_bytes"],
                        "disk_free_bytes": samples[-1]["disk_free_bytes"],
                        "real_orders_enabled": False,
                    }, sort_keys=True), flush=True)
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
                    log_path = work / f"q22_fanout_{next_idx + 1:02d}.log"
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
            if guarded_30m and result["sample_span_seconds"] < 1700:
                raise Q22Rejected("Q27_HOST_SAMPLE_SPAN_SHORTER_THAN_30M_SOURCE")
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
            failfast = True
            # Read the Unix operator even after HTTP 503, before the failure
            # handler terminates the private collector. Publish only fixed
            # enums, salted-by-origin session digest and numeric counters.
            fault_health = {}
            fault_metrics = {}
            fault_http = None
            try:
                fault_http, fault_health = read_private_status(
                    work / "v36_monitor.sock", "/api/v36/health"
                )
                _, fault_metrics = read_private_status(
                    work / "v36_monitor.sock", "/api/v36/metrics"
                )
                codes = named_failures(
                    fault_health, fault_metrics, prior,
                    health_http=fault_http,
                )
            except (OSError, ValueError, KeyError, TimeoutError):
                codes = ["PRIVATE_MONITOR_READ_FAILED"]
            if not codes:
                codes = ["UNKNOWN_CHECK_FAILED"]
            receipt = failure_receipt(
                fault_health, fault_metrics, codes, samples,
                time.monotonic() - started, len(windows), fault_http,
            )
            (public / "q23_redacted_fault_receipt.json").write_text(
                json.dumps(receipt, sort_keys=True, indent=2) + "\n"
            )
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
            if os.environ.get("Q24_WS_DIAG_ENABLED") == "1":
                # The probe remains in the 0700 source workdir. Read it only
                # after collector shutdown to include the final close/exit
                # event. Publish only schema-validated numeric counters.
                target = public / "q24_redacted_ws_close_receipt.json"
                try:
                    receipt = publish_receipt(
                        work / "q24_private_ws_close.jsonl", target
                    )
                    print("Q24_P0A_TRACE_CAPTURED " + json.dumps({
                        "event_count": receipt["events_observed"],
                        "sockets": receipt["observed_connections"],
                        "close_events": receipt["close_events"],
                        "error_events": receipt["websocket_error_events"],
                        "root_cause_proven": False,
                        "orders_enabled": False,
                    }, sort_keys=True), flush=True)
                except (OSError, ValueError) as exc:
                    # Missing or malformed telemetry MUST NOT be promoted to
                    # evidence of transport stability.
                    target.write_text(json.dumps({
                        "schema": "q24_p0a_public_close_forensics_v1",
                        "classification": "TRACE_UNAVAILABLE_OR_REJECTED",
                        "trace_schema_verified": False,
                        "failure_class": type(exc).__name__,
                        "real_orders_enabled": False,
                        "uninterrupted_five_hour_source_certified": False,
                    }, sort_keys=True) + "\n")
                    print("FAIL_Q24_P0A_PRIVATE_TRACE_UNVERIFIED", flush=True)
            if os.environ.get("Q27_HEARTBEAT_GUARD_ENABLED") == "1":
                # Q27 is explicitly opted in and always accompanied by the
                # Q25 erase-on-gap guard and Q24 close forensic trace.
                public_target = public / "q27_redacted_heartbeat_guard.json"
                try:
                    guard = publish_q27_receipt(
                        work / "q27_private_heartbeat_guard.jsonl",
                        public_target,
                    )
                    print("Q27_HEARTBEAT_GUARD_EVIDENCE " + json.dumps({
                        "sockets": guard["opened_sockets_constructed"],
                        "heartbeats_seen":
                            guard["last_heartbeat_count"],
                        "interventions": guard["interventions_total"],
                        "schema_verified": True,
                        "real_orders_enabled": False,
                    }, sort_keys=True), flush=True)
                except (OSError, ValueError) as exc:
                    public_target.write_text(json.dumps({
                        "schema": "q27_heartbeat_guard_public_receipt_v1",
                        "classification": "TRACE_UNAVAILABLE_OR_REJECTED",
                        "trace_schema_verified": False,
                        "error_class": type(exc).__name__,
                        "real_orders_enabled": False,
                        "continuous_5h_proven": False,
                    }, sort_keys=True) + "\n")
                    print("FAIL_Q27_HEARTBEAT_TRACE_NOT_VERIFIED",
                          flush=True)
            if os.environ.get("Q28_FORENSICS_ENABLED") == "1":
                # Evidence is collected AFTER termination. The normal Q24
                # and Q27 acceptance still requires fully clean telemetry.
                # On failure, Q28 may preserve only schema-validated,
                # newline-committed prefixes. Never use for admission.
                target = public / "q28_redacted_incident_receipt.json"
                result = publish_incident(
                    work / "q24_private_ws_close.jsonl",
                    work / "q27_private_heartbeat_guard.jsonl",
                    target,
                    collector_failed=failfast,
                )
                print("Q28_TERMINATION_CLASSIFICATION " + json.dumps({
                    "event_class": result["observed_termination_class"],
                    "q24_committed": result["q24_committed_events"],
                    "q27_committed": result["q27_committed_events"],
                    "q24_trace_validation": result["q24_trace_validation"],
                    "q27_trace_validation": result["q27_trace_validation"],
                    "source_certified": False,
                    "real_orders_enabled": False,
                }, sort_keys=True), flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private-workdir", type=Path, required=True)
    p.add_argument("--public-summary", type=Path, required=True)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--duration", type=int,
                   choices=[SMOKE_DURATION, 1800, CI_DURATION],
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
