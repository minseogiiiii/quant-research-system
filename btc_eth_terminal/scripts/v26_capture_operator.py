"""v26 opt-in, PUBLIC-ONLY Coinbase capture with bounded logs and clock trace.

Default is preflight-only. On a durable Linux host this tool samples chrony,
boot identity, UTC and CLOCK_MONOTONIC throughout capture, fsyncing every
sample. Host-generated evidence is NOT independent exchange attestation.
No broker, authentication or live order interface is present.
"""

from __future__ import annotations

import argparse
import os
import resource
import shutil
import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
def _q14_load_after_path_setup_0():
    from v25_capture_operator import COLLECTOR, exclusive_write, preflight, snapshot
    from v26_clock_trace import ZERO, canonical, record
    return COLLECTOR, exclusive_write, preflight, snapshot, ZERO, canonical, record

COLLECTOR, exclusive_write, preflight, snapshot, ZERO, canonical, record = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

MAX_LOG_BYTES = 64 * 1024 * 1024
INTERVAL = {"smoke": 20.0, "24h": 300.0}


def stream_sha256(path: Path):
    h = sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_trace_sample(stream, seq, previous, obs):
    item = record(seq, previous, obs)
    stream.write(canonical(item) + b"\n")
    stream.flush()
    os.fsync(stream.fileno())
    return item["hash"]


def stop_process(proc):
    if proc.poll() is not None:
        return proc.returncode
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return proc.wait(timeout=3)


def _enforce_child_log_size_limit():
    # Linux-only operator: bound each child log at kernel level even if the
    # producer floods stdout/stderr before the next periodic observation.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_LOG_BYTES, MAX_LOG_BYTES))


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--evidence-dir", type=Path, required=True)
    ap.add_argument("--mode", choices=["smoke", "24h"], default="smoke")
    ap.add_argument("--raw-budget", type=int, default=15_000_000_000)
    ap.add_argument("--archive-budget", type=int, default=20_000_000_000)
    ap.add_argument("--disk-reserve", type=int, default=4 * 1024**3)
    ap.add_argument("--execute-real-public-capture", action="store_true")
    args = ap.parse_args(argv)
    work = args.workdir
    evidence_dir = args.evidence_dir
    if work.is_symlink() or not work.is_dir():
        ap.error("Workdir must be an existing non-symlink directory")
    if evidence_dir.is_symlink() or evidence_dir.exists():
        ap.error("Evidence directory already exists or is a symlink")
    if evidence_dir.resolve().is_relative_to(work.resolve()):
        ap.error("Evidence dir must be outside source workdir")
    evidence_dir.mkdir(parents=True, mode=0o700)
    mode = "real_public_24h" if args.mode == "24h" else "real_public_smoke_60s"
    pre = preflight(work, mode, args.raw_budget, args.archive_budget, args.disk_reserve)
    exclusive_write(evidence_dir / "V26_PREFLIGHT.json", pre)
    if not args.execute_real_public_capture:
        print(
            "PREFLIGHT_ONLY:"
            + ("READY_NOT_EXECUTED" if pre["ready_for_real_capture"] else "BLOCKED")
        )
        return 0 if pre["ready_for_real_capture"] else 2
    if not pre["ready_for_real_capture"]:
        print("CAPTURE_BLOCKED:" + ",".join(pre["failures"]), file=sys.stderr)
        return 2
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
    source_parent = work / "soak_evidence"
    before_roots = (
        set(source_parent.glob("coinbase_btc_eth_soak_*"))
        if source_parent.exists()
        else set()
    )
    trace = evidence_dir / "V26_CLOCK_TRACE.jsonl"
    stdout_path = evidence_dir / "V26_STDOUT.log"
    stderr_path = evidence_dir / "V26_STDERR.log"
    failure = None
    rc = None
    observations = []
    with (
        trace.open("xb") as fp,
        stdout_path.open("xb") as out,
        stderr_path.open("xb") as err,
    ):
        previous = ZERO
        obs = snapshot(work)
        observations.append(obs)
        previous = write_trace_sample(fp, 0, previous, obs)
        proc = subprocess.Popen(
            [shutil.which("node"), str(COLLECTOR)],
            cwd=work,
            env=env,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=_enforce_child_log_size_limit,
        )
        try:
            # Only child process waits here. Every timeout also records fresh
            # host observations. Child stdout/stderr never reside in RAM.
            while True:
                try:
                    rc = proc.wait(timeout=INTERVAL[args.mode])
                except subprocess.TimeoutExpired:
                    rc = None
                obs = snapshot(work)
                observations.append(obs)
                previous = write_trace_sample(fp, len(observations) - 1, previous, obs)
                if out.tell() > MAX_LOG_BYTES or err.tell() > MAX_LOG_BYTES:
                    failure = "CAPTURE_LOG_LIMIT_EXCEEDED"
                    rc = stop_process(proc)
                    break
                mono_elapsed = (
                    obs["monotonic_ns"] - observations[0]["monotonic_ns"]
                ) / 1e9
                if mono_elapsed > duration + 90:
                    failure = "CAPTURE_RUNTIME_LIMIT_EXCEEDED"
                    rc = stop_process(proc)
                    break
                if rc is not None:
                    break
        except BaseException:
            stop_process(proc)
            raise
        out.flush()
        os.fsync(out.fileno())
        err.flush()
        os.fsync(err.fileno())
    new_roots = (
        set(source_parent.glob("coinbase_btc_eth_soak_*")) - before_roots
        if source_parent.exists()
        else set()
    )
    source = next(iter(new_roots)) if len(new_roots) == 1 else None
    manifest = source / "manifest.json" if source else None
    manifest_hash = (
        stream_sha256(manifest)
        if manifest is not None and not manifest.is_symlink() and manifest.is_file()
        else None
    )
    operator = {
        "schema": "v25_operator_evidence_v1",
        "run_mode": mode,
        "before": observations[0],
        "after": observations[-1],
        "source_archive_dir": str(source.resolve()) if source else None,
        "archive_manifest_sha256": manifest_hash,
        "capture_exit_code": rc,
        "injection_options_disabled": True,
        "producer_script_sha256": stream_sha256(COLLECTOR),
        "stdout_sha256": stream_sha256(stdout_path),
        "stderr_sha256": stream_sha256(stderr_path),
        "self_reported_host_evidence_only": True,
        "not_independent_exchange_attestation": True,
        "orders_enabled": False,
        "capture_failure": failure,
    }
    op_path = evidence_dir / "V25_OPERATOR_EVIDENCE.json"
    exclusive_write(op_path, operator)
    context = {
        "schema": "v26_clock_trace_context_v1",
        "run_mode": mode,
        "source_archive_dir": operator["source_archive_dir"],
        "archive_manifest_sha256": manifest_hash,
        "operator_evidence_sha256": stream_sha256(op_path),
        "clock_trace_sha256": stream_sha256(trace),
        "trace_samples": len(observations),
        "capture_failure": failure,
        "disk_reserve_bytes": args.disk_reserve,
        "internal_host_observation_only": True,
        "independent_exchange_attestation": False,
    }
    exclusive_write(evidence_dir / "V26_TRACE_CONTEXT.json", context)
    print(
        "HOST_OBSERVED_CAPTURE_EXIT:"
        + str(rc)
        + " trace_samples="
        + str(len(observations))
    )
    return 0 if rc == 0 and failure is None and manifest_hash else 2


if __name__ == "__main__":
    raise SystemExit(run())
