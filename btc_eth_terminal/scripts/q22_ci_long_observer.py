"""Q22 CI-only long observer for the UNCHANGED Node collector and v36 engine.

This is NOT the production 24h durable observer. The original observer's
40GB/55GB/10GiB durable policy stays intact. This process uses an explicit
smaller bounded policy on ephemeral Linux GitHub runners, with separate
read-only market observation and independent post-run validation.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from v36_observe_existing_collector import (  # noqa: E402
    COLLECTOR,
    LiveMarketEngine,
    capture_ready_for_research,
    observer_source_complete,
    read_tap,
    server_for_live,
    server_for_operator,
)

CI_DURATION = 18000
SMOKE_DURATION = 180
RAW_BUDGET = 10_000_000_000
ARCHIVE_BUDGET = 14_000_000_000
DISK_RESERVE = 4 * 1024**3
PREFLIGHT_MARGIN = 700_000_000
PRODUCTS = ("BTC-USD", "ETH-USD")


def ci_source_policy(seconds, raw, archive, reserve, free) -> bool:
    """CI-specific limit, NEVER accepted as production durable admission."""
    return (
        type(seconds) is int
        and seconds in (SMOKE_DURATION, CI_DURATION)
        and type(raw) is int
        and raw == RAW_BUDGET
        and type(archive) is int
        and archive == ARCHIVE_BUDGET
        and type(reserve) is int
        and reserve == DISK_RESERVE
        and type(free) is int
        and free >= archive + reserve + PREFLIGHT_MARGIN
    )


def evaluate_capture(
    working: Path, rc: int, engine, finished: bool, start: float
) -> dict:
    sources = list((working / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
    source = sources[0] if len(sources) == 1 else None
    manifest_path = source / "manifest.json" if source else None
    manifest_bytes = manifest_path.read_bytes() if (
        manifest_path and manifest_path.is_file()
    ) else None
    try:
        manifest = json.loads(manifest_bytes) if manifest_bytes else None
    except (TypeError, ValueError, UnicodeError):
        manifest = None
    health = engine.health()
    return {
        "schema": "q22_ci_observer_evidence_v1",
        "mode": "LIVE_SOURCE",
        "producer_script_sha256": sha256(COLLECTOR.read_bytes()).hexdigest(),
        "collector_exit_code": rc,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "captured_manifest_sha256": sha256(manifest_bytes).hexdigest()
        if manifest_bytes else None,
        "capture_ready_for_research": capture_ready_for_research(rc, manifest),
        "observer_reader_finished": finished,
        "observer_source_complete": observer_source_complete(
            manifest, health, finished
        ),
        "observer_health_before_stop": health,
        "capture_status": (
            manifest.get("status") if isinstance(manifest, dict) else None
        ),
        "capture_message_count": manifest.get("messages")
        if isinstance(manifest, dict) else None,
        "capture_fault_count": len(manifest.get("faults", []))
        if isinstance(manifest, dict) and isinstance(manifest.get("faults"), list)
        else None,
        "captured_dir": str(source) if source else None,
        "ci_ephemeral_runner_only": True,
        "durable_operator_mode": False,
        "execution_authorized": False,
        "trading_enabled": False,
        "persistent_host_24h_certified": False,
    }


def run(work: Path, seconds: int, port: int) -> int:
    if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("Q22_LINUX_SO_PEERCRED_REQUIRED")
    if not 1024 <= port <= 65535:
        raise RuntimeError("Q22_INVALID_LOOPBACK_PORT")
    if work.exists() or work.is_symlink():
        raise RuntimeError("Q22_REFUSE_REUSE_CAPTURE_DIRECTORY")
    if not work.parent.is_dir() or work.parent.is_symlink():
        raise RuntimeError("Q22_UNSAFE_CAPTURE_PARENT")
    free = shutil.disk_usage(work.parent).free
    if not ci_source_policy(
        seconds, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, free
    ):
        raise RuntimeError("Q22_CI_SOURCE_POLICY_OR_DISK_PREFLIGHT_FAILED")
    if not shutil.which("node"):
        raise RuntimeError("Q22_NODE_NOT_INSTALLED")
    script_hash = sha256(COLLECTOR.read_bytes()).hexdigest()
    work.mkdir(mode=0o700)
    q24_enabled = os.environ.get("Q24_WS_DIAG_ENABLED") == "1"
    if os.environ.get("Q24_WS_DIAG_ENABLED") not in (None, "1"):
        raise RuntimeError("Q24_WS_DIAG_FLAG_MUST_BE_EXPLICIT")
    q24_preload = ROOT / "scripts/q24_ws_close_probe.mjs"
    if q24_enabled and not q24_preload.is_file():
        raise RuntimeError("Q24_PRIVATE_DIAGNOSTIC_PRELOAD_MISSING")
    tap = work / "v36_observer.sock"
    monitor = work / "v36_monitor.sock"
    if max(len(os.fsencode(p)) for p in (tap, monitor)) > 100:
        raise RuntimeError("Q22_PRIVATE_SOCKET_PATH_TOO_LONG")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tap))
    os.chmod(tap, 0o600)
    listener.listen(1)
    listener.settimeout(2.0)
    engine = LiveMarketEngine(
        mode="LIVE_SOURCE", collector_runtime_verified=True,
        collector_script_sha256=script_hash,
    )
    gateway = server_for_live(engine, port=port)
    gateway_thread = Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    operator = server_for_operator(engine, gateway, monitor)
    operator_thread = Thread(target=operator.serve_forever, daemon=True)
    operator_thread.start()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SOAK_DURATION_SECONDS": str(seconds),
        "SOAK_RAW_BUDGET_BYTES": str(RAW_BUDGET),
        "SOAK_ARCHIVE_BUDGET_BYTES": str(ARCHIVE_BUDGET),
        "SOAK_DISK_RESERVE_BYTES": str(DISK_RESERVE),
        "SOAK_CHUNK_LIMIT_BYTES": "8000000",
        "SOAK_TEST_FIXTURE": "0",
        "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
        "V36_LIVE_TAP_SOCKET": str(tap),
    }
    if q24_enabled:
        env["Q24_WS_DIAG_PRIVATE_PATH"] = str(
            work.resolve() / "q24_private_ws_close.jsonl"
        )
    node_args = [shutil.which("node")]
    if q24_enabled:
        node_args.extend(["--import", str(q24_preload)])
    node_args.append(str(COLLECTOR))
    done = [False]
    proc = None
    worker = None
    finished = False
    start = time.monotonic()
    try:
        with (
            (work / "collector_stdout.log").open("xb") as stdout,
            (work / "collector_stderr.log").open("xb") as stderr,
        ):
            proc = subprocess.Popen(
                node_args,
                cwd=work, env=env, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
            try:
                while proc.poll() is None:
                    try:
                        client, _ = listener.accept()
                    except TimeoutError:
                        continue
                    peer = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    pid, uid, _gid = struct.unpack("3i", peer)
                    if (pid, uid) != (proc.pid, os.getuid()):
                        client.close()
                        engine.transport_lost("COLLECTOR_PEER_CREDENTIAL_MISMATCH")
                        continue
                    worker = Thread(
                        target=read_tap, args=(client, engine, done), daemon=True
                    )
                    worker.start()
                    break
                while proc.poll() is None:
                    time.sleep(0.2)
            except (KeyboardInterrupt, SystemExit):
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            if worker is not None:
                worker.join(timeout=10)
                finished = not worker.is_alive()
            evidence = evaluate_capture(work, proc.returncode, engine, finished, start)
            engine.stopped()
            stdout.flush()
            os.fsync(stdout.fileno())
            stderr.flush()
            os.fsync(stderr.fileno())
        with (work / "Q22_CI_OBSERVER_EVIDENCE.json").open("x") as result:
            json.dump(evidence, result, sort_keys=True, indent=2)
            result.write("\n")
            result.flush()
            os.fsync(result.fileno())
        print("Q22_OBSERVER_END " + json.dumps({
            "source_complete": evidence["observer_source_complete"],
            "capture_ready": evidence["capture_ready_for_research"],
            "exit_code": proc.returncode,
            "manifest_sha256": evidence["captured_manifest_sha256"],
            "duration": seconds,
            "orders_enabled": False,
        }, sort_keys=True), flush=True)
        return 0 if (
            evidence["capture_ready_for_research"]
            and evidence["observer_source_complete"]
            and evidence["captured_manifest_sha256"]
            and evidence["elapsed_seconds"] >= seconds - 5
        ) else 2
    finally:
        done[0] = True
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        operator.shutdown()
        operator.server_close()
        operator_thread.join(timeout=4)
        gateway.shutdown()
        gateway.server_close()
        gateway_thread.join(timeout=4)
        listener.close()
        for p in (tap, monitor):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--duration", type=int, choices=[SMOKE_DURATION, CI_DURATION],
                   required=True)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--execute-public-capture", action="store_true", required=True)
    args = p.parse_args(argv)
    try:
        return run(args.workdir, args.duration, args.port)
    except (RuntimeError, OSError, ValueError) as exc:
        print("FAIL_Q22_CI_SOURCE " + type(exc).__name__ + ":" +
              str(exc).splitlines()[0][:170], flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
