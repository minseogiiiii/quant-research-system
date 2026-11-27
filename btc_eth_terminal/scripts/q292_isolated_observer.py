"""Q29.2-E: one observer process per source, capability IPC, no trading.

The parent owns a private socketpair for each observer. The observer owns its
Node collector; Linux parent-death signaling prevents orphan capture on crash.
The host, disk, network and public Gateway remain shared failure domains.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from independent_soak_replay import strict_json
from q292_dual_gateway import PRODUCTS, ROOT, Collector, DualRoute, server_for_dual
from q292_failover_policy import Source
from q293_b4_diagnostics import reason_class

MAX_REPLY = 256 * 1024
IPC_TIMEOUT = 0.8
STARTUP_GRACE = 10.0
SOURCE_FIELDS = set(Source.__dataclass_fields__)


def receive_exact(sock, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OBSERVER_IPC_DEADLINE")
        sock.settimeout(remaining)
        piece = sock.recv(count - len(data))
        if not piece:
            raise EOFError("OBSERVER_IPC_EOF")
        data.extend(piece)
    return bytes(data)


def receive_reply(sock, deadline):
    size = struct.unpack("!I", receive_exact(sock, 4, deadline))[0]
    if not 0 < size <= MAX_REPLY:
        raise ValueError("OBSERVER_IPC_FRAME_BOUND")
    return strict_json(receive_exact(sock, size, deadline))


def send_reply(sock, value):
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if not 0 < len(data) <= MAX_REPLY:
        raise ValueError("OBSERVER_IPC_FRAME_BOUND")
    sock.sendall(struct.pack("!I", len(data)) + data)


def decode_observation(value, name, synthetic):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"source", "pair"}:
        raise ValueError("OBSERVER_IPC_SCHEMA")
    fields, pair = value["source"], value["pair"]
    if type(fields) is not dict or set(fields) != SOURCE_FIELDS:
        raise ValueError("OBSERVER_SOURCE_SCHEMA")
    source = Source(**fields)
    if source.name != name:
        raise ValueError("OBSERVER_SOURCE_IDENTITY")
    if not source.admissible(time.monotonic_ns(), 3_000_000_000):
        return None
    if type(pair) is not dict or set(pair) != set(PRODUCTS):
        raise ValueError("OBSERVER_PAIR_SCHEMA")
    mode = "ARCHIVE_SIMULATION" if synthetic else "LIVE_SOURCE"
    for product, row in pair.items():
        if (type(row) is not dict or row.get("product") != product
                or row.get("session_id") != source.session
                or row.get("mode") != mode
                or row.get("live_feed") is not (not synthetic)
                or row.get("trading_enabled") is not False
                or row.get("execution_authorized") is not False
                or row.get("status") != "VALID_OBSERVED_QUOTE"
                or type(row.get("state_version")) is not int
                or row["state_version"] <= 0
                or type(row.get("source_raw_sha256")) is not str
                or not re.fullmatch("[0-9a-f]{64}", row["source_raw_sha256"])
                or type(row.get("source_last_l2_receiver_ms")) is not int):
            raise ValueError("OBSERVER_PAIR_IDENTITY")
        age = time.time_ns() // 1_000_000 - row["source_last_l2_receiver_ms"]
        if not 0 <= age <= 3000:
            return None
    return source, pair


class ObserverProcess:
    """Independent process with a permanent quarantine on IPC protocol failure."""

    def __init__(self, name, work, duration, synthetic, *, fixture_fault=None):
        if fixture_fault not in (None, "duplicate", "sequence", "heartbeat"):
            raise ValueError("INVALID_FIXTURE_FAULT")
        if fixture_fault is not None and not synthetic:
            raise ValueError("FIXTURE_FAULT_REQUIRES_SYNTHETIC_MODE")
        self.name, self.work, self.synthetic = name, Path(work), synthetic
        self.failed = False
        self.last_reason = "OBSERVER_STARTING"
        self.closed = False
        self.start = time.monotonic()
        self.process = None
        self.sock, child = socket.socketpair()
        try:
            command = [
                sys.executable, str(ROOT / "scripts/q292_parent_bound_exec.py"),
                str(os.getpid()), sys.executable, str(Path(__file__).resolve()),
                "--worker", "--fd", str(child.fileno()), "--name", name,
                "--workdir", str(self.work), "--duration", str(duration),
                "--synthetic" if synthetic else "--execute-public-pilot",
            ]
            if fixture_fault is not None:
                command.extend(("--fixture-fault", fixture_fault))
            self.process = subprocess.Popen(
                command, pass_fds=(child.fileno(),), start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self.sock.close()
            raise
        finally:
            child.close()

    def observation(self):
        if self.failed or self.closed:
            return None
        if self.process.poll() is not None:
            self.last_reason = "OBSERVER_EXITED"
            return None
        # Startup can be slow on CI; do not issue a request before the worker
        # has installed its listener. Its private startup marker is not data.
        if not (self.work / "observer_ready").is_file():
            if time.monotonic() - self.start > STARTUP_GRACE:
                self.failed = True
                self.last_reason = "OBSERVER_STARTUP_TIMEOUT"
            return None
        deadline = time.monotonic() + IPC_TIMEOUT
        try:
            self.sock.settimeout(IPC_TIMEOUT)
            self.sock.sendall(b"R")
            reply = receive_reply(self.sock, deadline)
            if type(reply) is not dict or set(reply) != {"observation", "reason"}:
                raise ValueError("OBSERVER_REPLY_SCHEMA")
            self.last_reason = reason_class(reply["reason"])
            result = decode_observation(reply["observation"], self.name, self.synthetic)
            if result is None and reply["observation"] is not None:
                self.last_reason = "OBSERVATION_STALE"
            if self.process.poll() is not None:
                self.last_reason = "OBSERVER_EXITED"
                return None
            return result
        except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
            self.last_reason = ("IPC_TIMEOUT" if isinstance(exc, TimeoutError)
                                else "IPC_EOF" if isinstance(exc, EOFError)
                                else "IPC_OS_ERROR" if isinstance(exc, OSError)
                                else "IPC_SCHEMA_REJECTED")
            # A timed-out reply may arrive later. Never reuse this stream.
            self.failed = True
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
            return None

    def close(self):
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def worker(args):
    stopped = False

    def stop(_sig, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(fileno=args.fd) as sock:
        sock.settimeout(.25)
        child = None
        try:
            child = Collector(args.name, args.workdir, args.duration,
                              args.synthetic, parent_bound=True,
                              fixture_fault=args.fixture_fault)
            (args.workdir / "observer_ready").touch(mode=0o600, exist_ok=False)
            # Bounded even if the parent has a bug and never sends EOF.
            # Original collector starts duration after snapshots (45s setup)
            # and adds a five-second capture margin. Parent owns the hard cap.
            end = time.monotonic() + args.duration + 60
            while not stopped and time.monotonic() < end:
                try:
                    request = sock.recv(1)
                except TimeoutError:
                    continue
                if not request:
                    break
                if request != b"R":
                    raise ValueError("OBSERVER_IPC_COMMAND")
                observation = child.observation()
                result = None if observation is None else {
                    "source": asdict(observation[0]), "pair": observation[1],
                }
                sock.settimeout(IPC_TIMEOUT)
                send_reply(sock, {"observation": result, "reason": child.last_reason})
                sock.settimeout(.25)
        finally:
            if child is not None:
                child.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--fixture-fault", choices=("duplicate", "sequence", "heartbeat"))
    parser.add_argument("--fd", type=int)
    parser.add_argument("--name", choices=("primary", "secondary"))
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--port", type=int, default=8790)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--synthetic", action="store_true")
    modes.add_argument("--execute-public-pilot", action="store_true")
    args = parser.parse_args()
    if args.fixture_fault is not None and not (args.worker and args.synthetic):
        parser.error("fault injection requires a synthetic worker")
    if args.worker:
        if not 15 <= args.duration <= 18000:
            parser.error("worker duration outside bounded policy")
    elif (not 15 <= args.duration <= 180
            or args.execute_public_pilot and args.duration != 180):
        parser.error("only bounded direct pilot permitted")
    if args.worker:
        if args.fd is None or args.name is None:
            parser.error("worker requires private capability and source name")
        worker(args)
        return
    if args.fd is not None or args.name is not None:
        parser.error("worker-only flags")
    args.workdir.mkdir(mode=0o700)
    sources = []
    stopped = False

    def stop(_sig, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for name in ("primary", "secondary"):
            sources.append(ObserverProcess(
                name, args.workdir / name, args.duration, args.synthetic))
        route = DualRoute(*sources)
        with server_for_dual(route, args.port) as server:
            server.timeout = .2
            end = time.monotonic() + args.duration
            while not stopped and time.monotonic() < end:
                route.read()
                server.handle_request()
    finally:
        for source in sources:
            source.close()


if __name__ == "__main__":
    main()
