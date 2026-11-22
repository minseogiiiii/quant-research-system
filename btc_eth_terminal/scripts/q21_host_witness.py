"""External read-only Q21 Linux witness for an existing v36 durable capture.

Records bounded private Unix operator health every minute without touching the
collector, raw market data or exchange credentials. A passing synthetic test is
NOT a 24-hour source certificate. Real host evidence requires independent audit.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import stat
import sys
import time
from hashlib import sha256
from pathlib import Path

SCHEMA = "q21_linux_host_witness_v1"
SAMPLE_MAX_BYTES = 5_000_000
SHA = re.compile(r"[a-f0-9]{64}\Z")
TARGET_WITNESS_SECONDS = 85_700
MIN_WITNESS_SPAN_SECONDS = 85_200
DISK_RESERVE_BYTES = 10 * 1024**3


class WitnessRejected(ValueError):
    pass


def checked_socket(path: Path) -> Path:
    path = Path(path)
    p = path.lstat()
    parent = path.parent.lstat()
    if (
        not stat.S_ISSOCK(p.st_mode)
        or stat.S_ISLNK(p.st_mode)
        or p.st_uid != os.getuid()
        or p.st_mode & 0o077
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        raise WitnessRejected("UNSAFE_PRIVATE_OPERATOR_SOCKET")
    return path


def unix_json(path: Path, route: str) -> dict:
    checked_socket(path)
    if route not in ("/api/v36/health", "/api/v36/metrics"):
        raise WitnessRejected("UNREGISTERED_OPERATOR_ROUTE")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3.0)
        sock.connect(str(path))
        sock.sendall(("GET " + route + " HTTP/1.1\r\n"
                      "Host: localhost\r\nConnection: close\r\n\r\n").encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        length = response.getheader("Content-Length")
        if (response.status != 200 or response.getheader("Transfer-Encoding")
                or not length or not length.isascii() or not length.isdecimal()
                or not 0 < int(length) <= 32_768):
            raise WitnessRejected("PRIVATE_OPERATOR_HEALTH_FAILED")
        data = response.read(int(length) + 1)
        if len(data) != int(length):
            raise WitnessRejected("PRIVATE_OPERATOR_BODY_TRUNCATED")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise WitnessRejected("PRIVATE_OPERATOR_NONOBJECT")
        return value


def proc_state(pid: int) -> tuple[int, int]:
    if type(pid) is not int or pid <= 1:
        raise WitnessRejected("INVALID_OBSERVER_PID")
    root = Path("/proc") / str(pid)
    statfile = (root / "status").read_text()
    rss = next(
        (int(line.split()[1]) * 1024 for line in statfile.splitlines()
         if line.startswith("VmRSS:")),
        None,
    )
    if rss is None:
        raise WitnessRejected("OBSERVER_RSS_UNAVAILABLE")
    return rss, len(list((root / "fd").iterdir()))


def capture_sample(capture_dir: Path, observer_pid: int) -> dict:
    source = Path(capture_dir)
    if not source.is_dir() or source.is_symlink():
        raise WitnessRejected("UNSAFE_CAPTURE_WORKDIR")
    health = unix_json(source / "v36_monitor.sock", "/api/v36/health")
    transport = unix_json(source / "v36_monitor.sock", "/api/v36/metrics")
    session = health.get("session_id")
    last = health.get("last_receiver_time_ms")
    now_wall_ns = time.time_ns()
    rss, fds = proc_state(observer_pid)
    free = os.statvfs(source)
    disk_free = free.f_bavail * free.f_frsize
    if (
        health.get("operator_monitor_schema") != "v36_3_private_operator_monitor_v1"
        or health.get("mode") != "LIVE_SOURCE"
        or health.get("live_feed") is not True
        or health.get("status") != "HEALTHY"
        or health.get("operationally_healthy") is not True
        or health.get("trading_enabled") is not False
        or not isinstance(session, str)
        or not re.fullmatch(r"[a-f0-9]{32}", session)
        or not isinstance(last, int)
        or isinstance(last, bool)
        or not 0 <= now_wall_ns // 1_000_000 - last <= 15_000
        or set(health.get("snapshots", [])) != {"BTC-USD", "ETH-USD"}
        or set(health.get("subscriptions", []))
        != {"level2", "market_trades", "heartbeats"}
        or health.get("connection_gaps") != 0
        or health.get("connection_id") != 0
        or transport.get("monitor_transport") != "UNIX_SOCKET"
        or transport.get("operationally_healthy") is not True
        or transport.get("trading_enabled") is not False
        or transport.get("execution_authorized") is not False
        or transport.get("max_sse_clients") != 8
        or type(transport.get("active_sse_connections")) is not int
        or not 0 <= transport["active_sse_connections"] <= 8
        or disk_free < DISK_RESERVE_BYTES
        or rss > 2 * 1024**3
        or fds > 4096
    ):
        raise WitnessRejected("REAL_SOURCE_HOST_HEALTH_CONTRACT_FAILURE")
    for key in ("messages", "heartbeats", "last_sequence"):
        if type(health.get(key)) is not int or health[key] < 0:
            raise WitnessRejected("BAD_SOURCE_PROGRESS_COUNTER")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[a-f0-9-]{36}", boot_id):
        raise WitnessRejected("BAD_LINUX_BOOT_ID")
    return {
        "schema": SCHEMA,
        "index": -1,
        "wall_time_ns": now_wall_ns,
        "monotonic_ns": time.monotonic_ns(),
        "boot_id_sha256": sha256(boot_id.encode()).hexdigest(),
        "observer_pid": observer_pid,
        "source_session_sha256": sha256(session.encode()).hexdigest(),
        "messages": health["messages"],
        "last_sequence": health["last_sequence"],
        "heartbeats": health["heartbeats"],
        "connection_gaps": 0,
        "source_age_ms": now_wall_ns // 1_000_000 - last,
        "active_sse_connections": transport["active_sse_connections"],
        "observer_rss_bytes": rss,
        "observer_open_fds": fds,
        "disk_free_bytes": disk_free,
        "trading_enabled": False,
        "synthetic_data": False,
    }


def verify_sequence(
    rows: list[dict], minimum_span: int = MIN_WITNESS_SPAN_SECONDS
) -> dict:
    if (not isinstance(rows, list) or len(rows) < 2 or
            not 0 < minimum_span <= 86400):
        raise WitnessRejected("WITNESS_SAMPLE_COUNT_OR_SPAN_INVALID")
    first = rows[0]
    fixed = ("boot_id_sha256", "source_session_sha256", "observer_pid")
    for i, row in enumerate(rows):
        if (
            not isinstance(row, dict)
            or row.get("schema") != SCHEMA
            or row.get("index") != i
            or row.get("trading_enabled") is not False
            or row.get("synthetic_data") is not False
            or row.get("connection_gaps") != 0
            or any(row.get(key) != first.get(key) for key in fixed)
            or any(not SHA.fullmatch(str(row.get(key, ""))) for key in fixed[:2])
            or type(row.get("observer_pid")) is not int
            or type(row.get("source_age_ms")) is not int
            or not 0 <= row["source_age_ms"] <= 15000
            or type(row.get("disk_free_bytes")) is not int
            or row["disk_free_bytes"] < DISK_RESERVE_BYTES
            or type(row.get("observer_rss_bytes")) is not int
            or not 0 <= row["observer_rss_bytes"] <= 2 * 1024**3
            or type(row.get("observer_open_fds")) is not int
            or not 0 <= row["observer_open_fds"] <= 4096
            or type(row.get("active_sse_connections")) is not int
            or not 0 <= row["active_sse_connections"] <= 8
            or any(type(row.get(key)) is not int or row[key] < 0
                   for key in ("wall_time_ns", "monotonic_ns", "messages",
                               "last_sequence", "heartbeats"))
            or row["messages"] <= 0
            or row["last_sequence"] != row["messages"] - 1
        ):
            raise WitnessRejected("WITNESS_SAMPLE_VIOLATION:" + str(i))
        if i:
            prior = rows[i - 1]
            monotonic_gap = row["monotonic_ns"] - prior["monotonic_ns"]
            wall_gap = row["wall_time_ns"] - prior["wall_time_ns"]
            if (not 0 < monotonic_gap <= 120_000_000_000
                    or abs(wall_gap - monotonic_gap) > 15_000_000_000
                    or row["messages"] <= prior["messages"]
                    or row["heartbeats"] < prior["heartbeats"]):
                raise WitnessRejected("WITNESS_CONTINUITY_FAILURE:" + str(i))
    span_ns = rows[-1]["monotonic_ns"] - first["monotonic_ns"]
    if span_ns < minimum_span * 1_000_000_000:
        raise WitnessRejected("INSUFFICIENT_PERSISTENT_HOST_WITNESS_DURATION")
    return {
        "schema": "q21_witness_sequence_admission_v1",
        "observed_seconds": round(span_ns / 1e9, 3),
        "samples": len(rows),
        "single_observer_process": True,
        "single_linux_boot": True,
        "single_source_session": True,
        "sample_gap_max_seconds": 120,
        "order_routing_authorized": False,
        "persistent_host_24h_certified": False,
        "independent_operator_approval": False,
    }


def witness(capture_dir: Path, pid: int, output: Path, seconds: int, interval: int):
    if not 3600 <= seconds <= TARGET_WITNESS_SECONDS or not 30 <= interval <= 90:
        raise WitnessRejected("INVALID_OPERATOR_WITNESS_SCHEDULE")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise WitnessRejected("REFUSE_OVERWRITE_WITNESS")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(mode=0o600, exist_ok=False)
    started = time.monotonic()
    index = 0
    with output.open("a", encoding="utf-8") as writer:
        while True:
            record = capture_sample(capture_dir, pid)
            record["index"] = index
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            writer.write(encoded + "\n")
            writer.flush()
            os.fsync(writer.fileno())
            index += 1
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                break
            time.sleep(min(interval, max(0, seconds - elapsed)))
    print("Q21_WITNESS_RECORDED_NO_APPROVAL " + json.dumps({
        "samples": index,
        "sha256": sha256(output.read_bytes()).hexdigest(),
        "witness_file": str(output),
        "trading_enabled": False,
        "persistent_host_certified": False,
    }, sort_keys=True))


def audit_witness(path: Path, expected_sha256: str):
    if not SHA.fullmatch(expected_sha256):
        raise WitnessRejected("EXTERNAL_WITNESS_DIGEST_REQUIRED")
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WitnessRejected("UNSAFE_WITNESS_FILE")
    if not 0 < info.st_size <= SAMPLE_MAX_BYTES:
        raise WitnessRejected("UNBOUNDED_WITNESS_FILE")
    content = Path(path).read_bytes()
    if sha256(content).hexdigest() != expected_sha256:
        raise WitnessRejected("WITNESS_EXTERNAL_SHA_MISMATCH")
    rows = [json.loads(line) for line in content.splitlines()]
    if len(rows) < 1200 or len(rows) > 3000:
        raise WitnessRejected("INSUFFICIENT_OR_EXCESSIVE_WITNESS_SAMPLES")
    return verify_sequence(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--capture-workdir", required=True, type=Path)
    record.add_argument("--observer-pid", required=True, type=int)
    record.add_argument("--out", required=True, type=Path)
    record.add_argument("--seconds", type=int, default=TARGET_WITNESS_SECONDS)
    record.add_argument("--interval", type=int, default=60)
    audit = sub.add_parser("audit-witness")
    audit.add_argument("--samples", type=Path, required=True)
    audit.add_argument("--external-sha256", required=True)
    args = p.parse_args(argv)
    try:
        if args.command == "record":
            if not sys.platform.startswith("linux"):
                raise WitnessRejected("LINUX_ONLY")
            witness(args.capture_workdir, args.observer_pid, args.out,
                    args.seconds, args.interval)
        else:
            report = audit_witness(args.samples, args.external_sha256)
            print("PASS_Q21_WITNESS_ONLY_NOT_24H_CERTIFIED " +
                  json.dumps(report, sort_keys=True))
    except (WitnessRejected, OSError, ValueError, json.JSONDecodeError) as exc:
        print("FAIL_Q21_HOST_WITNESS " + type(exc).__name__ + ":" +
              str(exc).splitlines()[0][:180])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
