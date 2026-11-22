"""One-shot, real public-feed capture operator. NO exchange order functionality.

Default mode is preflight only. Live mode requires --execute-real-public-capture,
works only on a user-provisioned persistent host with real DNS, and records
host-reported (NOT independent exchange-attested) time observations.
No automatic reconnect stitching, no synthetic fixtures, no live trading.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
COLLECTOR = HERE / "scripts" / "btc_eth_multiday_v20.mjs"


def utc():
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def digest(data):
    return sha256(data).hexdigest()


def run_command(argv, timeout=5):
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None, "", ""


def chrono_observation():
    status, output, _ = run_command(["chronyc", "tracking"])
    if status != 0:
        return {
            "method": "unavailable",
            "command_exit_code": status,
            "leap_status": None,
            "stratum": None,
            "system_offset_seconds": None,
        }

    def extract(pattern):
        m = re.search(pattern, output, re.I | re.M)
        return m.group(1).strip() if m else None

    stratum = extract(r"^Stratum\s*:\s*(\d+)\s*$")
    # chrony output: System time : 0.000123456 seconds slow of NTP time
    offset = extract(
        r"^System time\s*:\s*([0-9.]+)\s+seconds\s+(fast|slow)\s+of\s+NTP time\s*$"
    )
    if offset is not None:
        text = re.search(
            r"^System time\s*:\s*([0-9.]+)\s+seconds\s+(fast|slow)\s+of\s+NTP time\s*$",
            output,
            re.I | re.M,
        )
        offset_value = float(text.group(1)) * (
            -1 if text.group(2).lower() == "slow" else 1
        )
    else:
        offset_value = None
    return {
        "method": "chronyc_tracking_host_reported",
        "command_exit_code": status,
        "leap_status": extract(r"^Leap status\s*:\s*([A-Za-z ]+)\s*$"),
        "stratum": int(stratum) if stratum else None,
        "system_offset_seconds": offset_value,
    }


def snapshot(root):
    try:
        socket.getaddrinfo(
            "advanced-trade-ws.coinbase.com", 443, type=socket.SOCK_STREAM
        )
        dns = "RESOLVED_NO_WEBSOCKET_ATTESTATION"
    except OSError:
        dns = "FAILED_DNS_LOOKUP"
    boot = Path("/proc/sys/kernel/random/boot_id")
    try:
        boot_id_digest = digest(boot.read_bytes()) if boot.is_file() else None
    except OSError:
        boot_id_digest = None
    storage = shutil.disk_usage(root)
    return {
        "observed_utc": utc(),
        "monotonic_ns": time.monotonic_ns(),
        "boot_id_digest": boot_id_digest,
        "storage_root": str(root.resolve()),
        "free_storage_bytes": storage.free,
        "dns_status": dns,
        "clock": chrono_observation(),
    }


def preflight(root, mode, raw_budget, archive_budget, reserve):
    failures = []
    if root.is_symlink() or not root.is_dir() or not os.access(root, os.W_OK):
        failures.append("INVALID_PERSISTENT_WRITABLE_WORKDIR")
    node = shutil.which("node")
    rc, version, _ = run_command([node, "--version"]) if node else (None, "", "")
    if (
        rc != 0
        or not re.fullmatch(r"v\d+\.\d+\.\d+", version)
        or int(version[1:].split(".")[0]) < 22
    ):
        failures.append("NODE_22_MISSING")
    if sys.version_info < (3, 11):
        failures.append("PYTHON_3_11_MISSING")
    if not COLLECTOR.is_file():
        failures.append("SOURCE_COLLECTOR_MISSING")
    if (
        not 1_024 <= raw_budget <= 60 * 10**9
        or not raw_budget <= archive_budget <= 80 * 10**9
    ):
        failures.append("INVALID_STORAGE_BUDGETS")
    if not 0 <= reserve <= 10**12:
        failures.append("INVALID_RESERVE")
    observation = snapshot(root)
    if observation["free_storage_bytes"] < archive_budget + reserve:
        failures.append("INSUFFICIENT_FREE_BYTES_FOR_FULL_ARCHIVE_BUDGET_AND_RESERVE")
    if observation["dns_status"] != "RESOLVED_NO_WEBSOCKET_ATTESTATION":
        failures.append("PUBLIC_COINBASE_DNS_UNAVAILABLE")
    sys.path.insert(0, str(HERE / "backend"))
    from v25_capture_admission import clock_pass

    if not clock_pass(observation["clock"]):
        failures.append("HOST_REPORTED_CHRONY_NOT_READY")
    if observation["boot_id_digest"] is None:
        failures.append("BOOT_ID_NOT_AVAILABLE")
    return {
        "schema": "v25_host_preflight_v1",
        "mode": mode,
        "timestamp_utc": utc(),
        "node_version": version or None,
        "source_script_sha256": digest(COLLECTOR.read_bytes())
        if COLLECTOR.exists()
        else None,
        "root": str(root.resolve()),
        "observation": observation,
        "failures": failures,
        "ready_for_real_capture": not failures,
        "host_durable_24h_proven": False,
        "websocket_authenticated_market_truth": False,
        "live_orders_enabled": False,
    }


def exclusive_write(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(obj, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", required=True, type=Path)
    p.add_argument("--evidence-dir", required=True, type=Path)
    p.add_argument("--mode", choices=["smoke", "24h"], default="smoke")
    p.add_argument("--execute-real-public-capture", action="store_true")
    p.add_argument("--raw-budget", type=int, default=15_000_000_000)
    p.add_argument("--archive-budget", type=int, default=20_000_000_000)
    p.add_argument("--disk-reserve", type=int, default=4 * 1024**3)
    args = p.parse_args()
    if args.workdir.is_symlink() or not args.workdir.is_dir():
        raise SystemExit("Provide an existing, non-symlink writable persistent workdir")
    if args.evidence_dir.is_symlink() or args.evidence_dir.exists():
        raise SystemExit("Evidence output directory must not already exist")
    if args.evidence_dir.resolve().is_relative_to(args.workdir.resolve()):
        raise SystemExit("Write observer evidence OUTSIDE collector working directory")
    args.evidence_dir.mkdir(parents=True, mode=0o700)
    mode = "real_public_24h" if args.mode == "24h" else "real_public_smoke_60s"
    pre = preflight(
        args.workdir, mode, args.raw_budget, args.archive_budget, args.disk_reserve
    )
    exclusive_write(args.evidence_dir / "V25_PREFLIGHT.json", pre)
    if not args.execute_real_public_capture:
        print(
            "PREFLIGHT_ONLY:"
            + ("READY_BUT_NOT_EXECUTED" if pre["ready_for_real_capture"] else "BLOCKED")
        )
        return 0 if pre["ready_for_real_capture"] else 2
    if not pre["ready_for_real_capture"]:
        raise SystemExit("Refusing real capture: " + ",".join(pre["failures"]))
    # Never inherit malicious fixture and Node injection environment knobs.
    duration = 86400 if args.mode == "24h" else 60
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SOAK_DURATION_SECONDS": str(duration),
        "SOAK_RAW_BUDGET_BYTES": str(args.raw_budget),
        "SOAK_ARCHIVE_BUDGET_BYTES": str(args.archive_budget),
        "SOAK_DISK_RESERVE_BYTES": str(args.disk_reserve),
        "SOAK_CHUNK_LIMIT_BYTES": "8000000",
        "SOAK_TEST_FIXTURE": "0",
        "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
    }
    before = snapshot(args.workdir)
    roots_before = (
        set((args.workdir / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        if (args.workdir / "soak_evidence").exists()
        else set()
    )
    node = shutil.which("node")
    stdout, stderr, exitcode = "", "", None
    try:
        result = subprocess.run(
            [node, str(COLLECTOR)],
            cwd=args.workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=duration + 90,
        )
        stdout, stderr, exitcode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        stdout = (
            (error.stdout or b"").decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            (error.stderr or b"").decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        exitcode = 124
    after = snapshot(args.workdir)
    new_roots = (
        set((args.workdir / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        - roots_before
    )
    source = next(iter(new_roots)) if len(new_roots) == 1 else None
    manifest_file = source / "manifest.json" if source else None
    manifest_sha = (
        digest(manifest_file.read_bytes())
        if manifest_file and manifest_file.is_file() and not manifest_file.is_symlink()
        else None
    )
    evidence = {
        "schema": "v25_operator_evidence_v1",
        "run_mode": mode,
        "before": before,
        "after": after,
        "archive_manifest_sha256": manifest_sha,
        "source_archive_dir": str(source) if source else None,
        "capture_exit_code": exitcode,
        "injection_options_disabled": True,
        "producer_script_sha256": digest(COLLECTOR.read_bytes()),
        "stdout_sha256": digest(stdout.encode()),
        "stderr_sha256": digest(stderr.encode()),
        "stdout_excerpt": stdout[-1000:],
        "stderr_excerpt": stderr[-1000:],
        "self_reported_host_evidence_only": True,
        "not_independent_exchange_attestation": True,
        "orders_enabled": False,
    }
    exclusive_write(args.evidence_dir / "V25_OPERATOR_EVIDENCE.json", evidence)
    print("CAPTURE_FINISHED_AND_OBSERVED:" + str(exitcode))
    if exitcode != 0 or manifest_sha is None:
        raise SystemExit(2)
    # The independent admission gate must still be run on persisted files.
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    raise SystemExit(main())
