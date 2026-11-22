"""Opt-in read-only v36 observer for the UNCHANGED v29 underlying Node collector.

The v29 operator uses scripts/btc_eth_multiday_v20.mjs. This tool executes
that exact existing collector with only an OPTIONAL local Unix tap added. Raw
archive writes, budgets, chunk hashes and manifest rules remain independent
of UI availability. No credentials or real order APIs. Linux SO_PEERCRED is
required to prove the producer PID for a live admission attempt.

Example: python scripts/v36_observe_existing_collector.py --execute-public-capture \
  --workdir /private/new/empty/workdir --duration 120 --port 8766
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


def _q14_load_after_path_setup_0():
    from v35_replay_service import StateRejected
    from v36_live_engine import MAX_LINE, LiveMarketEngine
    from v36_live_gateway import server_for_live
    from v36_operator_monitor import server_for_operator

    return (
        StateRejected,
        MAX_LINE,
        LiveMarketEngine,
        server_for_live,
        server_for_operator,
    )


StateRejected, MAX_LINE, LiveMarketEngine, server_for_live, server_for_operator = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0

COLLECTOR = ROOT / "scripts/btc_eth_multiday_v20.mjs"


def read_tap(connection: socket.socket, engine: LiveMarketEngine, done: list):
    buffer = bytearray()
    try:
        connection.settimeout(2.0)
        while not done[0]:
            try:
                data = connection.recv(1 << 16)
            except TimeoutError:
                continue
            if not data:
                engine.transport_lost("COLLECTOR_OBSERVATION_SOCKET_CLOSED")
                return
            buffer.extend(data)
            if len(buffer) > MAX_LINE + 65536:
                engine.transport_lost("LOCAL_OBSERVER_BACKPRESSURE_EXCEEDED")
                return
            while True:
                idx = buffer.find(b"\n")
                if idx < 0:
                    break
                line = bytes(buffer[: idx + 1])
                del buffer[: idx + 1]
                if line.startswith(b"V36_TAP_CONTROL "):
                    try:
                        payload = json.loads(line[len(b"V36_TAP_CONTROL ") :])
                        if (
                            len(line) > 512
                            or payload.get("schema") != "v36_tap_transport_control_v1"
                            or payload.get("status") != "SOURCE_GAP"
                            or type(payload.get("connection_id")) is not int
                            or payload["connection_id"] != engine.connection_id
                        ):
                            raise ValueError("INVALID_TRANSPORT_CONTROL")
                    except (ValueError, TypeError, AttributeError):
                        engine.transport_lost("INVALID_OBSERVER_TRANSPORT_CONTROL")
                        return
                    engine.transport_lost("COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP")
                    # The next source must provide a fresh conn_id, sequence
                    # zero, both snapshots, acks, heartbeat and valid L2.
                    continue
                try:
                    engine.consume(line)
                except StateRejected:
                    # The only safe continuity action is a new full source
                    # connection. The collector continues archiving regardless.
                    engine.transport_lost("LIVE_ADMISSION_REJECTED")
                    return
    except OSError as exc:
        engine.transport_lost("OBSERVER_SOCKET_FAILURE:" + type(exc).__name__)
    finally:
        with contextlib.suppress(OSError):
            connection.close()


def capture_ready_for_research(collector_rc: int, manifest: object) -> bool:
    """Fail-closed on an empty or incomplete capture even if child exits 0.

    This does not grant LIVE admission. It is a strict post-capture archive
    readiness check, not exchange attestation or independent external pinning.
    """
    return (
        collector_rc == 0
        and isinstance(manifest, dict)
        and manifest.get("format") == "btc_eth_raw_soak_v10"
        and manifest.get("status")
        == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and manifest.get("ready_for_research") is True
        and type(manifest.get("messages")) is int
        and manifest["messages"] > 0
        and manifest.get("faults") == []
        and manifest.get("complete_sequence_per_connection") is True
        and manifest.get("all_connections_have_snapshots") is True
    )


def observer_source_complete(
    manifest: object, health: object, reader_finished: bool
) -> bool:
    """Prove the observer consumed the FULL finalized capture, not only a snapshot.

    Source-readiness alone is insufficient if the optional tap drops packets.
    This is host-level continuity evidence, not signed exchange attestation.
    """
    if (
        not reader_finished
        or not isinstance(manifest, dict)
        or not isinstance(health, dict)
    ):
        return False
    connections = manifest.get("connections")
    if not isinstance(connections, list) or len(connections) != 1:
        return False
    c = connections[0]
    if not isinstance(c, dict):
        return False
    return (
        health.get("mode") == "LIVE_SOURCE"
        and health.get("live_feed") is True
        and health.get("trading_enabled") is False
        and type(manifest.get("messages")) is int
        and manifest["messages"] > 0
        and type(health.get("messages")) is int
        and health["messages"] == manifest["messages"] == c.get("messages")
        and type(health.get("last_sequence")) is int
        and health["last_sequence"]
        == c.get("last_sequence")
        == manifest["messages"] - 1
        and health.get("connection_id") == c.get("id") == 0
        and health.get("connection_gaps") == 0
        and health.get("heartbeats") == c.get("heartbeat_count")
        and type(health.get("heartbeats")) is int
        and health["heartbeats"] > 0
        and set(health.get("snapshots", [])) == {"BTC-USD", "ETH-USD"}
        and set(health.get("subscriptions", []))
        == {"level2", "market_trades", "heartbeats"}
    )


DURABLE_MIN_RAW_BYTES = 40_000_000_000
DURABLE_MIN_ARCHIVE_BYTES = 55_000_000_000
DURABLE_MIN_DISK_RESERVE_BYTES = 10 * 1024**3


def durable_policy_valid(duration: int, raw: int, archive: int, reserve: int) -> bool:
    """Hard budget contract for opt-in persistent Linux source capture.

    Passing this function does NOT certify host capacity, source continuity
    or 24-hour operational success.
    """
    return (
        type(duration) is int
        and 3600 <= duration <= 86400
        and type(raw) is int
        and raw >= DURABLE_MIN_RAW_BYTES
        and type(archive) is int
        and archive >= max(DURABLE_MIN_ARCHIVE_BYTES, raw)
        and type(reserve) is int
        and reserve >= DURABLE_MIN_DISK_RESERVE_BYTES
    )


def durable_disk_capacity_sufficient(
    capture_parent: Path, archive: int, reserve: int
) -> bool:
    """Fail closed before creating a 24h capture on an undersized filesystem.

    This is a conservative *preflight*, not proof that disk space remains
    free during the run. The Node collector still enforces each write's
    reserve and archive bounds. The operator must provision extra margin.
    """
    if (
        type(archive) is not int
        or type(reserve) is not int
        or archive < DURABLE_MIN_ARCHIVE_BYTES
        or reserve < DURABLE_MIN_DISK_RESERVE_BYTES
    ):
        return False
    parent = Path(capture_parent)
    if not parent.is_dir() or parent.is_symlink():
        return False
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        return False
    return free >= archive + reserve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--execute-public-capture", action="store_true")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument(
        "--fault-test-only",
        action="store_true",
        help="Never promote deliberately faulted source",
    )
    ap.add_argument(
        "--durable-operator-mode",
        action="store_true",
        help="Strict 40GB/55GB/10GiB Linux limits",
    )
    ap.add_argument("--inject-disconnect-after-messages", type=int, default=0)
    ap.add_argument("--raw-budget-bytes", type=int, default=15_000_000_000)
    ap.add_argument("--archive-budget-bytes", type=int, default=20_000_000_000)
    ap.add_argument("--disk-reserve-bytes", type=int, default=4 * 1024**3)
    args = ap.parse_args(argv)
    if args.durable_operator_mode and (
        args.fault_test_only or args.inject_disconnect_after_messages
    ):
        ap.error("Durable source recording cannot use fault injection")
    if args.durable_operator_mode and not durable_policy_valid(
        args.duration,
        args.raw_budget_bytes,
        args.archive_budget_bytes,
        args.disk_reserve_bytes,
    ):
        ap.error(
            "Durable mode requires 1..24h, >=40GB raw, >=55GB archive, >=10GiB reserve"
        )
    if args.inject_disconnect_after_messages and (
        not args.fault_test_only
        or not 100 <= args.inject_disconnect_after_messages <= 10000
    ):
        ap.error("Fault injection requires --fault-test-only and 100..10000 messages")
    if not args.execute_public_capture:
        print("BLOCKED: explicit --execute-public-capture required", file=sys.stderr)
        return 2
    if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
        print("BLOCKED: Linux SO_PEERCRED unavailable", file=sys.stderr)
        return 2
    if (
        not 15 <= args.duration <= (86400 if args.durable_operator_mode else 3600)
        or not 1024 <= args.port <= 65535
    ):
        ap.error("duration out of mode range or invalid port")
    if (
        args.raw_budget_bytes < 1024
        or args.archive_budget_bytes < args.raw_budget_bytes
        or args.disk_reserve_bytes < 4 * 1024**3
    ):
        ap.error("Storage budgets must preserve raw<=archive and >=4GiB disk reserve")
    work = args.workdir
    if args.durable_operator_mode and not durable_disk_capacity_sufficient(
        work.parent, args.archive_budget_bytes, args.disk_reserve_bytes
    ):
        ap.error(
            
                'INSUFFICIENT_DURABLE_DISK_CA'
                'PACITY: 55GB archive budget '
                'plus 10GiB reserve must be f'
                'ree before startup'
            
        )
    if work.exists() or work.is_symlink():
        ap.error("Workdir MUST NOT exist (no preexisting raw evidence can be reused)")
    if not shutil.which("node"):
        ap.error("Node 22+ must be installed")
    original = COLLECTOR.read_bytes()
    script_hash = sha256(original).hexdigest()
    work.mkdir(parents=True, mode=0o700)
    tap_path = work / "v36_observer.sock"
    if len(os.fsencode(tap_path)) > 100:
        ap.error("Private observation socket path too long")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tap_path))
    os.chmod(tap_path, 0o600)
    listener.listen(1)
    listener.settimeout(2.0)
    engine = LiveMarketEngine(
        mode="LIVE_SOURCE",
        collector_runtime_verified=True,
        collector_script_sha256=script_hash,
    )
    server = server_for_live(engine, port=args.port)
    gateway_thread = Thread(target=server.serve_forever, daemon=True)
    gateway_thread.start()
    # Operator health/metrics must remain reachable even when eight SSE
    # subscriptions plus two slow HTTP headers occupy every browser slot.
    operator_path = work / "v36_monitor.sock"
    operator_server = server_for_operator(engine, server, operator_path)
    operator_thread = Thread(target=operator_server.serve_forever, daemon=True)
    operator_thread.start()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SOAK_DURATION_SECONDS": str(args.duration),
        "SOAK_RAW_BUDGET_BYTES": str(args.raw_budget_bytes),
        "SOAK_ARCHIVE_BUDGET_BYTES": str(args.archive_budget_bytes),
        "SOAK_DISK_RESERVE_BYTES": str(args.disk_reserve_bytes),
        "SOAK_CHUNK_LIMIT_BYTES": "8000000",
        "SOAK_TEST_FIXTURE": "0",
        "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": str(
            args.inject_disconnect_after_messages
        ),
        "V36_LIVE_TAP_SOCKET": str(tap_path),
    }
    done = [False]
    proc = None
    worker = None
    observer_finished = False
    observer_health = None
    start = time.monotonic()
    try:
        with (
            (work / "collector_stdout.log").open("xb") as stdout,
            (work / "collector_stderr.log").open("xb") as stderr,
        ):
            proc = subprocess.Popen(
                [shutil.which("node"), str(COLLECTOR)],
                cwd=work,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                while proc.poll() is None:
                    try:
                        client, _ = listener.accept()
                    except TimeoutError:
                        continue
                    # Verify same-user exact child PID, not an arbitrary local
                    # process pretending to supply accepted exchange messages.
                    peer = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    pid, uid, _gid = struct.unpack("3i", peer)
                    if pid != proc.pid or uid != os.getuid():
                        client.close()
                        engine.transport_lost("COLLECTOR_PEER_CREDENTIAL_MISMATCH")
                        continue
                    worker = Thread(
                        target=read_tap, args=(client, engine, done), daemon=True
                    )
                    worker.start()
                    # The collector tap is intentionally single-connection.
                    # Dropping it does NOT cause capture termination.
                    break
                while proc.poll() is None:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
            # Accepted multi-megabyte socket writes may remain in-flight
            # after the collector exits. Wait for the tap reader before
            # asserting full observer continuity.
            if worker is not None:
                worker.join(timeout=8.0)
                observer_finished = not worker.is_alive()
            observer_health = engine.health()
            engine.stopped()
            stdout.flush()
            os.fsync(stdout.fileno())
            stderr.flush()
            os.fsync(stderr.fileno())
        candidates = list((work / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        source = candidates[0] if len(candidates) == 1 else None
        manifest = source / "manifest.json" if source else None
        manifest_bytes = (
            manifest.read_bytes() if manifest and manifest.is_file() else None
        )
        source_hash = (
            sha256(manifest_bytes).hexdigest() if manifest_bytes is not None else None
        )
        try:
            capture_meta = (
                json.loads(manifest_bytes) if manifest_bytes is not None else None
            )
        except (ValueError, UnicodeError):
            capture_meta = None
        capture_ready = capture_ready_for_research(proc.returncode, capture_meta)
        observer_complete = observer_source_complete(
            capture_meta, observer_health, observer_finished
        )
        evidence = {
            "schema": "v36_observer_evidence_v1",
            "mode": "LIVE_SOURCE",
            "source_attestation": "HOST_PID_AND_CLOCK_ONLY_NOT_SIGNED_EXCHANGE",
            "producer_script_sha256": script_hash,
            "collector_exit_code": proc.returncode,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "captured_manifest_sha256": source_hash,
            "capture_ready_for_research": capture_ready,
            "fault_test_only": args.fault_test_only,
            "durable_operator_mode": args.durable_operator_mode,
            "injected_disconnect_after_messages": args.inject_disconnect_after_messages,
            "observer_reader_finished": observer_finished,
            "observer_source_complete": observer_complete,
            "observer_health_before_stop": observer_health,
            "capture_status": capture_meta.get("status")
            if isinstance(capture_meta, dict)
            else None,
            "capture_message_count": capture_meta.get("messages")
            if isinstance(capture_meta, dict)
            else None,
            "capture_fault_count": len(capture_meta.get("faults", []))
            if isinstance(capture_meta, dict)
            and isinstance(capture_meta.get("faults"), list)
            else None,
            "captured_dir": str(source) if source else None,
            "final_observer_health": engine.health(),
            "execution_authorized": False,
            "trading_enabled": False,
        }
        p = work / "V36_OBSERVER_EVIDENCE.json"
        with p.open("x") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(evidence, sort_keys=True))
        return (
            0
            if not args.fault_test_only
            and capture_ready
            and observer_complete
            and source_hash
            else 2
        )
    finally:
        done[0] = True
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        operator_server.shutdown()
        operator_server.server_close()
        operator_thread.join(timeout=3)
        with contextlib.suppress(FileNotFoundError):
            operator_path.unlink()
        server.shutdown()
        server.server_close()
        gateway_thread.join(timeout=3)
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            tap_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
