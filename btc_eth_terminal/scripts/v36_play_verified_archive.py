"""Run a V36 archive simulation with actual, externally SHA-pinned Coinbase JSONL.

NEVER grants LIVE_SOURCE. Intended for read-only local UI and E2E parity.
Source chunks are opened with O_NOFOLLOW, read fully into bounded bytes,
rehash-checked against the verified manifest BEFORE any event is ingested.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from hashlib import sha256
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from v35_archive_ingress import MAX_CHUNK_BYTES, ArchiveRejected, verify_capture
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live


def admitted_verified_chunks(capture: Path, pin: str):
    certificate = verify_capture(capture, pin)
    if (
        certificate["admission"] != "VERIFIED_ARCHIVE_ENVELOPE"
        or not certificate["source_eligible_for_state_replay"]
    ):
        raise ArchiveRejected("SOURCE_NOT_ELIGIBLE_FOR_ARCHIVE_SIMULATION")
    # Re-read the independently pinned manifest. No malicious rename can
    # authorize a different manifest between initial gate and chunk reads.
    manifest_raw = (capture / "manifest.json").read_bytes()
    if sha256(manifest_raw).hexdigest() != pin:
        raise ArchiveRejected("MANIFEST_CHANGED_AFTER_ADMISSION")
    manifest = json.loads(manifest_raw)
    yield certificate, None
    for chunk in manifest["chunks"]:
        p = capture / chunk["file"]
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            meta = os.fstat(fd)
            if (
                not 0 < meta.st_size <= MAX_CHUNK_BYTES
                or meta.st_size != chunk["archived_line_bytes"]
            ):
                raise ArchiveRejected("SOURCE_SIZE_CHANGED_AFTER_ADMISSION")
            raw = os.read(fd, meta.st_size + 1)
            if (
                len(raw) != meta.st_size
                or sha256(raw).hexdigest() != chunk["archive_sha256"]
            ):
                raise ArchiveRejected("SOURCE_BYTES_CHANGED_AFTER_ADMISSION")
        finally:
            os.close(fd)
        yield None, raw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", required=True, type=Path)
    ap.add_argument("--manifest-sha256", required=True)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument(
        "--speed",
        type=float,
        default=20,
        help="Receiver-clock acceleration; 0 = immediate",
    )
    ap.add_argument("--hold-seconds", type=int, default=30)
    args = ap.parse_args(argv)
    if (
        not 1024 <= args.port <= 65535
        or not 0 <= args.speed <= 1000
        or not 0 <= args.hold_seconds <= 3600
    ):
        ap.error("port/speed/hold out of bounds")
    engine = LiveMarketEngine(mode="ARCHIVE_SIMULATION")
    iterator = admitted_verified_chunks(args.capture, args.manifest_sha256)
    certificate, _ = next(iterator)
    server = server_for_live(engine, port=args.port)
    stop = [False]

    def on_signal(_sig, _frame):
        stop[0] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        json.dumps(
            {
                "status": "ARCHIVE_INGRESS_VERIFIED",
                "mode": "ARCHIVE_SIMULATION",
                "manifest_sha256": certificate["manifest_sha256"],
                "http_origin": f"http://127.0.0.1:{args.port}",
                "trading_enabled": False,
            }
        ),
        flush=True,
    )
    count = 0
    first_ms = None
    wall_start = time.monotonic()
    try:
        for _, chunk_bytes in iterator:
            for line in chunk_bytes.splitlines(keepends=True):
                if stop[0]:
                    break
                if args.speed > 0:
                    # Synchronize replay to archived receiver times rather than
                    # inventing new exchange timestamps for synthetic pacing.
                    recv = json.loads(line)["received_utc"]
                    from datetime import datetime

                    recv_ms = int(
                        datetime.fromisoformat(recv.replace("Z", "+00:00")).timestamp()
                        * 1000
                    )
                    if first_ms is None:
                        first_ms = recv_ms
                    due = (recv_ms - first_ms) / 1000 / args.speed
                    while time.monotonic() - wall_start < due:
                        if stop[0]:
                            break
                        time.sleep(min(0.05, due - (time.monotonic() - wall_start)))
                engine.consume(line)
                count += 1
            if stop[0]:
                break
        print(
            json.dumps(
                {
                    "status": "ARCHIVE_SIMULATION_FINISHED"
                    if not stop[0]
                    else "OPERATOR_INTERRUPTED",
                    "mode": engine.mode,
                    "messages": count,
                    "health": engine.health(),
                    "manifest_sha256": certificate["manifest_sha256"],
                }
            ),
            flush=True,
        )
        end = time.monotonic() + args.hold_seconds
        while not stop[0] and time.monotonic() < end:
            time.sleep(0.1)
    finally:
        engine.stopped()
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)
    return 0 if not stop[0] and count == certificate["messages"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
