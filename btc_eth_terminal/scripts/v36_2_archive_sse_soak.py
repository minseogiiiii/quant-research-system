"""Bounded real-archive SSE stress harness. Never claims actual LIVE messages.

Read one immutable one-hour Coinbase archive in receiver-time order, paced to a
requested duration (default 180s), over the existing v36 Market State and
loopback Gateway. Assert source/product isolation, cursor monotonicity, fail-
closed source loss and resource metrics. No raw source is uploaded or modified.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psutil
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live
from v36_play_verified_archive import admitted_verified_chunks


class ObservedEngine(LiveMarketEngine):
    def __init__(self):
        super().__init__(mode="ARCHIVE_SIMULATION")
        self.publication_times = {}

    def _publish(self, product, receiver_ms, seq):
        previous = self.version
        super()._publish(product, receiver_ms, seq)
        if self.version != previous:
            self.publication_times[self.version] = time.monotonic_ns()
            if len(self.publication_times) > 20000:
                # Only delivery-latency metrics use this bounded ephemeral map.
                for key in list(self.publication_times)[:10000]:
                    self.publication_times.pop(key, None)


def read_stream(url, client, stop, result):
    last = -1
    session = None
    events = 0
    messages = 0
    disconnected = False
    _ = None
    deadline = time.monotonic() + 250
    headers = {}
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            with urlopen(Request(url, headers=headers), timeout=8) as response:
                if response.status != 200:
                    raise AssertionError("SSE_HTTP_NOT_200")
                event = None
                payload = None
                cursor = None
                while not stop.is_set():
                    line = response.readline(250_001)
                    if not line:
                        raise AssertionError("UNEXPECTED_SSE_EOF")
                    if len(line) >= 250_001:
                        raise AssertionError("UNBOUNDED_SSE_LINE")
                    line = line.decode("utf-8").rstrip("\r\n")
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("id: "):
                        cursor = line[4:]
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                    elif not line and event:
                        if event == "source":
                            if session is not None and session != payload["session_id"]:
                                raise AssertionError("CROSS_SOURCE_RESUME")
                            session = payload["session_id"]
                            if not session or payload["product"] != client["product"]:
                                raise AssertionError("INVALID_SOURCE_HANDSHAKE")
                        elif event in ("snapshot", "state"):
                            if (
                                payload["product"] != client["product"]
                                or payload["session_id"] != session
                            ):
                                raise AssertionError("CROSS_PRODUCT_OR_SESSION_STATE")
                            expected_cursor = f"v36:{session}:{client['product']}:{
                                payload['state_version']
                            }"
                            if cursor != expected_cursor:
                                raise AssertionError("CURSOR_MISMATCH")
                            if payload["state_version"] <= last:
                                raise AssertionError("DUPLICATE_OR_OUT_OF_ORDER")
                            last = payload["state_version"]
                            headers = {"Last-Event-ID": cursor}
                            if event == "state":
                                messages += 1
                                t = client["engine"].publication_times.get(last)
                                if t:
                                    delay = max(0, time.monotonic_ns() - t) / 1e6
                                    result["delays_ms"].append(delay)
                            if (
                                client.get("force_reconnect")
                                and messages >= 25
                                and not disconnected
                            ):
                                disconnected = True
                                break
                            if client.get("intentionally_slow") and messages == 1:
                                time.sleep(12)
                        elif event == "resync_required":
                            result["resync_events"] += 1
                            result[
                                "resync_pre_gap"
                                if not client["source_done"].is_set()
                                else "resync_post_gap"
                            ] += 1
                            result["rows"] = messages
                            result["events"] = events
                            result["last_version"] = last
                            return
                        elif event == "stream_rotation":
                            break
                        events += 1
                        event = payload = cursor = None
                    if disconnected:
                        break
            if not client.get("force_reconnect") or not disconnected:
                break
            result["reconnects"] += 1
            disconnected = False
            client["force_reconnect"] = False
        result["rows"] = messages
        result["events"] = events
        result["last_version"] = last
    except Exception as exc:
        result["error"] = type(exc).__name__ + ":" + str(exc)[:120]


def percentile(items, p):
    if not items:
        return None
    a = sorted(items)
    return round(a[min(len(a) - 1, round((len(a) - 1) * p))], 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, type=Path)
    ap.add_argument("--manifest-sha256", required=True)
    ap.add_argument("--duration-seconds", type=int, default=180)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if not 60 <= args.duration_seconds <= 1200:
        ap.error("duration 60 to 1200 seconds")
    gen = admitted_verified_chunks(args.capture, args.manifest_sha256)
    certificate, _ = next(gen)
    engine = ObservedEngine()
    server = server_for_live(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stop = threading.Event()
    source_done = threading.Event()
    process = psutil.Process()
    stats = {
        "mode": "ARCHIVE_SIMULATION_NOT_LIVE",
        "schema": "v36_2_replay_sse_soak_v1",
        "source_manifest_sha256": args.manifest_sha256,
        "original_source_messages": certificate["messages"],
        "duration_target_seconds": args.duration_seconds,
        "raw_source_unchanged": True,
        "trading_enabled": False,
        "subscribers": {},
        "rss_samples_bytes": [],
        "fd_samples": [],
    }
    start = time.monotonic()
    clock0 = None
    count = 0
    clients = []

    def launch_clients():
        for name, product, slow, reconn in [
            ("BTC1", "BTC-USD", False, False),
            ("BTC2", "BTC-USD", False, True),
            ("BTC3", "BTC-USD", False, False),
            ("ETH1", "ETH-USD", False, False),
            ("ETH2", "ETH-USD", False, False),
            ("ETH3", "ETH-USD", True, False),
        ]:
            result = {
                "delays_ms": [],
                "resync_events": 0,
                "resync_pre_gap": 0,
                "resync_post_gap": 0,
                "reconnects": 0,
                "rows": 0,
                "events": 0,
                "last_version": -1,
            }
            stats["subscribers"][name] = result
            config = {
                "product": product,
                "engine": engine,
                "force_reconnect": reconn,
                "intentionally_slow": slow,
                "source_done": source_done,
            }
            u = f"http://127.0.0.1:{server.server_address[1]}/api/v36/stream?product={
                product
            }"
            t = threading.Thread(
                target=read_stream, args=(u, config, stop, result), daemon=True
            )
            clients.append(t)
            t.start()

    try:
        elapsed_since_last_sample = 0
        client_started = False
        for _, chunk in gen:
            for line in chunk.splitlines(keepends=True):
                data = json.loads(line)
                ms = int(
                    datetime.fromisoformat(
                        data["received_utc"].replace("Z", "+00:00")
                    ).timestamp()
                    * 1000
                )
                if clock0 is None:
                    clock0 = ms
                due = (ms - clock0) / 1000 / (3600 / args.duration_seconds)
                delay = due - (time.monotonic() - start)
                if delay > 0:
                    time.sleep(min(delay, 0.05))
                engine.consume(line)
                count += 1
                if not client_started and engine.health()["status"] == "HEALTHY":
                    launch_clients()
                    client_started = True
                now = time.monotonic() - start
                if now - elapsed_since_last_sample >= 1.0:
                    stats["rss_samples_bytes"].append(process.memory_info().rss)
                    stats["fd_samples"].append(process.num_fds())
                    elapsed_since_last_sample = now
            # Source chunks are never rewritten, only read and reverified.
        stats["feed_elapsed_seconds"] = round(time.monotonic() - start, 3)
        stats["fed_messages"] = count
        # Old session cannot be advertised after source transport loss.
        source_done.set()
        engine.transport_lost("EXPLICIT_ARCHIVE_REPLAY_TRANSPORT_LOSS")
        stats["last_health_after_fault"] = engine.health()["status"]
        url = f"http://127.0.0.1:{
            server.server_address[1]
        }/api/v36/stream?product=BTC-USD"
        try:
            urlopen(url, timeout=4)
            stats["post_gap_sse_status"] = 200
        except HTTPError as e:
            stats["post_gap_sse_status"] = e.code
        time.sleep(2)
    finally:
        stop.set()
        for t in clients:
            t.join(timeout=6)
        stats["gateway_metrics"] = server.metrics()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    stats["clients_with_error"] = {
        k: v["error"] for k, v in stats["subscribers"].items() if "error" in v
    }
    stats["resync_before_gap"] = {
        k: v["resync_pre_gap"]
        for k, v in stats["subscribers"].items()
        if v["resync_pre_gap"]
    }
    stats["resync_after_gap"] = {
        k: v["resync_post_gap"]
        for k, v in stats["subscribers"].items()
        if v["resync_post_gap"]
    }
    stats["sum_state_events"] = sum(
        v.get("rows", 0) for v in stats["subscribers"].values()
    )
    stats["pub_to_loopback_delivery_ms"] = {
        "p50": percentile(
            [d for v in stats["subscribers"].values() for d in v["delays_ms"]], 0.5
        ),
        "p95": percentile(
            [d for v in stats["subscribers"].values() for d in v["delays_ms"]], 0.95
        ),
        "p99": percentile(
            [d for v in stats["subscribers"].values() for d in v["delays_ms"]], 0.99
        ),
    }
    stats["fast_delivery_ms"] = {
        "p50": percentile(
            [
                d
                for k, v in stats["subscribers"].items()
                if k != "ETH3"
                for d in v["delays_ms"]
            ],
            0.5,
        ),
        "p95": percentile(
            [
                d
                for k, v in stats["subscribers"].items()
                if k != "ETH3"
                for d in v["delays_ms"]
            ],
            0.95,
        ),
        "p99": percentile(
            [
                d
                for k, v in stats["subscribers"].items()
                if k != "ETH3"
                for d in v["delays_ms"]
            ],
            0.99,
        ),
    }
    stats["rss_peak_bytes"] = max(stats["rss_samples_bytes"], default=None)
    stats["rss_delta_bytes"] = (
        (stats["rss_samples_bytes"][-1] - stats["rss_samples_bytes"][0])
        if stats["rss_samples_bytes"]
        else None
    )
    stats["fd_peak"] = max(stats["fd_samples"], default=None)
    stats["rss_samples_count"] = len(stats["rss_samples_bytes"])
    stats["status"] = (
        "PASS"
        if (
            count == certificate["messages"]
            and stats["post_gap_sse_status"] == 409
            and stats["last_health_after_fault"] == "RECONNECTING"
            and not stats["clients_with_error"]
            and all(v.get("rows", 0) > 0 for v in stats["subscribers"].values())
            and stats["subscribers"]["BTC2"].get("reconnects", 0) >= 1
            and not any(k != "ETH3" for k in stats["resync_before_gap"])
            and stats["gateway_metrics"]["peak_connections"] <= 8
        )
        else "FAIL"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in stats.items()
                if k not in ("rss_samples_bytes", "fd_samples", "subscribers")
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if stats["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
