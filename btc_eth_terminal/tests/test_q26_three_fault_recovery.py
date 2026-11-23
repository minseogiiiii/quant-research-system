"""Q26 P0-C: the original unchanged collector under three isolated faults.

All source traffic is deterministic SYNTHETIC websocket mock data; real orders
and 5h source certification are never implied. A genuine Coinbase 180s smoke
is a separate CI stage with no injected failures.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]

from q24_ws_diagnostic_receipt import read_private_trace  # noqa: E402
from q25_session_isolation_guard import SourceEpochIsolatedEngine  # noqa: E402
from v36_live_gateway import server_for_live  # noqa: E402
from v36_observe_existing_collector import read_tap  # noqa: E402

PRODUCTS = ("BTC-USD", "ETH-USD")
FAULTS = {
    "NETWORK_CLOSE": ("CLOSE:1006", 1006, False),
    "SEQUENCE_GAP": ("SOURCE:Error: SEQUENCE_GAP", 1000, True),
    "ALL_FEED_SILENCE": ("STALE_FEED", 1000, True),
    "HEARTBEAT_ONLY_SILENCE": ("CLOSE:1000", 1000, True),
}
MAX_RECONNECT_MS = 4000
MAX_RECOVERY_MS = 5000


def request(url: str, headers=None):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def until(check, deadline_seconds: float, *, tick=None):
    stop = time.monotonic() + deadline_seconds
    while time.monotonic() < stop:
        if tick is not None:
            tick()
        if check():
            return True
        time.sleep(0.025)
    return False


class IncidentEngine(SourceEpochIsolatedEngine):
    def __init__(self):
        super().__init__()
        self.first_ready = threading.Event()
        self.discontinuity = threading.Event()
        self.restored = threading.Event()
        self.incidents = []
        self.ready_time_ns = {}
        self.first_session = None
        self.old_version = None

    def _publish(self, product, receiver_ms, seq):
        super()._publish(product, receiver_ms, seq)
        if self.status != "HEALTHY" or not self.rows[product]:
            return
        if self.connection_id not in self.ready_time_ns:
            self.ready_time_ns[self.connection_id] = time.monotonic_ns()
        if self.connection_id == 0 and not self.first_ready.is_set():
            self.first_session = self.session_id
            self.old_version = self.rows[product][-1]["state_version"]
            self.first_ready.set()
        if self.connection_id == 1:
            self.restored.set()

    def _invalid(self, reason: str, phase: str = "QUARANTINED"):
        with self._cv:
            super()._invalid(reason, phase)
            sanitized = reason.split(":", 1)[0]
            if sanitized not in {
                "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP", "HEARTBEAT_STALE",
                "SOURCE_ENVELOPE_REJECTED", "NO_COLLECTOR_MESSAGES_WITHIN_STALENESS_BUDGET",
                "PRODUCT_L2_STALE_OR_CLOCK_ANOMALY", "COLLECTOR_OBSERVATION_SOCKET_CLOSED",
                "SOURCE_STOPPED", "LIVE_ADMISSION_REJECTED", "SOURCE_TRANSPORT_LOST",
            }:
                sanitized = "OTHER_REDACTED"
            if phase in ("STALE", "RECONNECTING", "QUARANTINED"):
                incident = {
                    "kind": sanitized, "phase": phase,
                    "monotonic_ns": time.monotonic_ns(),
                    "buffer_scrubbed": self.health()["q25_quote_buffers_scrubbed"],
                    "old_session_revoked": all(
                        self.snapshot(p)["state"] is None for p in PRODUCTS
                    ),
                }
                self.incidents.append(incident)
                self.discontinuity.set()


class ThreeFaultRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node 22 unavailable")

    def run_scenario(self, scenario: str):
        if scenario not in FAULTS:
            raise AssertionError("UNKNOWN_TEST_SCENARIO")
        expected_reason, expected_code, expected_local = FAULTS[scenario]
        with tempfile.TemporaryDirectory(prefix="q26_fault_") as directory:
            work = Path(directory)
            private = work / "private_ws_trace.jsonl"
            tap = work / "private_observer.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tap))
            listener.listen(1)
            listener.settimeout(4.0)
            engine = IncidentEngine()
            server = server_for_live(engine)
            http_thread = threading.Thread(
                target=server.serve_forever, daemon=True
            )
            http_thread.start()
            host = "http://127.0.0.1:" + str(server.server_address[1])
            done = [False]
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": "35",
                "SOAK_RAW_BUDGET_BYTES": "1000000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "SOAK_CHUNK_LIMIT_BYTES": "8000000",
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
                "V36_TAP_FIXTURE_MODE": "1",
                "V36_LIVE_TAP_SOCKET": str(tap),
                "Q26_TEST_SCENARIO": scenario,
                "Q24_WS_DIAG_PRIVATE_PATH": str(private),
            }
            args = [
                shutil.which("node"), "--import",
                str(ROOT / "tests/q26_three_fault_coinbase_mock.mjs"),
            ]
            if scenario == "HEARTBEAT_ONLY_SILENCE":
                args.extend([
                    "--import", str(
                        ROOT / "tests/q26_synthetic_heartbeat_watchdog.mjs"
                    ),
                ])
            args.extend([
                "--import", str(ROOT / "scripts/q24_ws_close_probe.mjs"),
                str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
            ])
            proc = subprocess.Popen(
                args, cwd=work, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            worker = None
            stream = None
            try:
                sock, _ = listener.accept()
                worker = threading.Thread(
                    target=read_tap, args=(sock, engine, done), daemon=True
                )
                worker.start()
                self.assertTrue(
                    until(engine.first_ready.is_set, 3, tick=engine.health),
                    "First session never became HEALTHY",
                )
                old_session = engine.first_session
                version = engine.old_version
                self.assertIsNotNone(old_session)
                for product in PRODUCTS:
                    status, body = request(
                        host + "/api/v36/snapshot?product=" + product
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(body["state"]["session_id"], old_session)
                stream = urlopen(
                    host + "/api/v36/stream?product=BTC-USD", timeout=3
                )
                self.assertEqual(stream.status, 200)
                self.assertTrue(
                    until(engine.discontinuity.is_set, 16, tick=engine.health),
                    "Collector or engine never rejected faulted session",
                )
                self.assertGreaterEqual(len(engine.incidents), 1)
                self.assertTrue(all(
                    x["buffer_scrubbed"] and x["old_session_revoked"]
                    for x in engine.incidents
                ))
                self.assertTrue(
                    until(
                        lambda: engine.health()["connection_id"] == 1
                        and engine.health()["status"] == "WAITING_FOR_SNAPSHOT"
                        and engine.health()["snapshots"] == ["BTC-USD"],
                        4, tick=engine.health,
                    ),
                    "Partial second-session BTC snapshot was not observed",
                )
                partial = engine.health()
                self.assertNotEqual(partial["session_id"], old_session)
                self.assertEqual(partial["connection_gaps"], 1)
                for product in PRODUCTS:
                    status, body = request(
                        host + "/api/v36/snapshot?product=" + product
                    )
                    self.assertEqual(status, 503)
                    self.assertIsNone(body["state"])
                    self.assertEqual(
                        request(host + "/api/v36/candles?product=" + product)[1]["rows"],
                        [],
                    )
                old_state = (
                    "/api/v36/state?product=BTC-USD&session=" +
                    old_session + "&version=" + str(version)
                )
                self.assertEqual(request(host + old_state)[0], 409)
                self.assertEqual(request(
                    host + "/api/v36/stream?product=BTC-USD"
                )[0], 409)
                self.assertTrue(
                    until(engine.restored.is_set, 4, tick=engine.health),
                    "New epoch failed dual snapshots, acks and heartbeat",
                )
                fresh = engine.health()
                self.assertEqual(fresh["status"], "HEALTHY")
                self.assertEqual(fresh["connection_id"], 1)
                self.assertNotEqual(fresh["session_id"], old_session)
                self.assertEqual(fresh["snapshots"], list(PRODUCTS))
                self.assertEqual(
                    fresh["subscriptions"],
                    ["heartbeats", "level2", "market_trades"],
                )
                self.assertGreater(fresh["heartbeats"], 0)
                self.assertGreaterEqual(fresh["q25_revocations_total"], 1)
                for product in PRODUCTS:
                    status, body = request(
                        host + "/api/v36/snapshot?product=" + product
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        body["state"]["session_id"], fresh["session_id"]
                    )
                    self.assertEqual(body["state"]["connection_id"], 1)
                self.assertEqual(request(host + old_state)[0], 409)
                old_cursor = (
                    "v36:" + old_session + ":BTC-USD:" + str(version)
                )
                self.assertEqual(
                    request(host + "/api/v36/stream?product=BTC-USD",
                            {"Last-Event-ID": old_cursor})[0],
                    409,
                )
                old_stream_wire = bytearray()
                for _ in range(30):
                    line = stream.readline()
                    if not line:
                        break
                    old_stream_wire.extend(line)
                    if b"event: resync_required" in old_stream_wire:
                        break
                self.assertIn(
                    b"event: resync_required", old_stream_wire,
                    "Old SSE should not silently continue across the gap",
                )
                self.assertFalse(engine.health()["q25_orders_enabled"])
            finally:
                if stream is not None:
                    stream.close()
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                output, errors = proc.communicate(timeout=8)
                done[0] = True
                if worker:
                    worker.join(timeout=4)
                server.shutdown()
                server.server_close()
                http_thread.join(timeout=4)
                listener.close()
                # Exactly one synthetic watchdog marker is required for
                # heartbeat-only source silence, NEVER for other failures.
                self.assertEqual(
                    b"Q26_SYNTHETIC_HEARTBEAT_WATCHDOG_INTERVENED"
                    in output,
                    scenario == "HEARTBEAT_ONLY_SILENCE",
                )
                # Neither process log is ever copied to public receipts.
                del output, errors
            self.assertFalse(worker and worker.is_alive())
            base = next(
                (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
            )
            meta = json.loads((base / "manifest.json").read_bytes())
            self.assertTrue(meta["synthetic_fixture"])
            self.assertFalse(meta["ready_for_research"])
            self.assertGreaterEqual(len(meta["connections"]), 2)
            self.assertEqual(meta["total_reconnects"], 1)
            self.assertTrue(any(
                fault.get("connection_id") == 0
                and fault.get("reason") == expected_reason
                for fault in meta["faults"]
            ), "Unexpected private collector fault classification")
            self.assertTrue(all(
                chunk["connection_id"] in (0, 1) for chunk in meta["chunks"]
            ))
            trace = read_private_trace(private)
            first_close = next(e for e in trace if (
                e["event"] == "SOCKET_CLOSE"
                and e["connection_index"] == 0
            ))
            self.assertEqual(first_close["close_code"], expected_code)
            self.assertEqual(
                first_close["local_close_requested"], expected_local
            )
            constructed = [
                x for x in trace if x["event"] == "SOCKET_CONSTRUCTED"
            ]
            self.assertEqual([x["connection_index"] for x in constructed], [0, 1])
            self.assertIsNotNone(constructed[1]["reconnect_delay_ms"])
            self.assertGreaterEqual(constructed[1]["reconnect_delay_ms"], 0)
            self.assertLessEqual(
                constructed[1]["reconnect_delay_ms"], MAX_RECONNECT_MS
            )
            initial_incident = engine.incidents[0]
            recovered_ms = (
                engine.ready_time_ns[1] - initial_incident["monotonic_ns"]
            ) // 1_000_000
            if scenario == "ALL_FEED_SILENCE":
                # The engine's 5s heartbeat stale state precedes the original
                # 12s collector any-message watchdog, so measure both clocks.
                collector_gap = next(x for x in engine.incidents if (
                    x["kind"] == "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP"
                ))
                collector_restore_ms = (
                    engine.ready_time_ns[1] - collector_gap["monotonic_ns"]
                ) // 1_000_000
                self.assertLessEqual(collector_restore_ms, MAX_RECOVERY_MS)
                self.assertGreaterEqual(recovered_ms, 6000)
            else:
                collector_restore_ms = recovered_ms
                self.assertLessEqual(recovered_ms, MAX_RECOVERY_MS)
            return {
                "scenario": scenario,
                "data_origin": "SYNTHETIC_LOCAL_WEBSOCKET_FIXTURE",
                "observed_fault_class": {
                    "NETWORK_CLOSE": "UNEXPECTED_WS_CLOSE_1006",
                    "SEQUENCE_GAP": "SOURCE_SEQUENCE_GAP",
                    "ALL_FEED_SILENCE": "ALL_SOURCE_FRAMES_STALE_FEED",
                    "HEARTBEAT_ONLY_SILENCE":
                        "HEARTBEAT_ONLY_SILENCE_SYNTHETIC_WATCHDOG_CLOSE",
                }[scenario],
                "collector_reported_expected_class": True,
                "first_source_session_revoked": True,
                "both_books_scrubbed": True,
                "partial_resnapshot_hidden": True,
                "prior_sse_resync_required": True,
                "old_session_cursor_rejected": True,
                "new_dual_snapshots_confirmed": True,
                "fresh_epoch_connection_id": 1,
                "fresh_epoch_uninterrupted_global": False,
                "elapsed_from_first_invalidation_to_recovery_ms": int(
                    recovered_ms
                ),
                "elapsed_from_collector_gap_to_recovery_ms": int(
                    collector_restore_ms
                ),
                "websocket_reconnect_delay_ms": constructed[1][
                    "reconnect_delay_ms"
                ],
                "native_ping_pong_observed": False,
                "heartbeat_only_watchdog_synthetic": (
                    scenario == "HEARTBEAT_ONLY_SILENCE"
                ),
                "original_collector_native_heartbeat_only_reconnect_proven":
                    False,
                "real_orders_enabled": False,
                "five_hour_certified": False,
            }

    def _record(self, name: str):
        summary = self.run_scenario(name)
        folder = os.environ.get("Q26_PUBLIC_SYNTHETIC_RECEIPTS")
        if folder:
            public = Path(folder)
            if not public.is_dir() or public.is_symlink():
                raise AssertionError("UNSAFE_Q26_PUBLIC_SUMMARY_DIRECTORY")
            dest = public / ("q26_" + name.lower() + "_synthetic.json")
            with dest.open("x") as fp:
                json.dump({
                    "schema": "q26_source_recovery_fault_v1",
                    "verdict": "PASS_SYNTHETIC_ONLY",
                    **summary,
                }, fp, sort_keys=True, indent=2)
                fp.write("\n")

    def test_network_abnormal_ws_close_reconnect(self):
        self._record("NETWORK_CLOSE")

    def test_sequence_gap_closes_and_restarts_fresh_session(self):
        self._record("SEQUENCE_GAP")

    def test_all_feed_heartbeat_silence_reconnect_after_native_timeout(self):
        self._record("ALL_FEED_SILENCE")

    def test_heartbeat_only_l2_continues_with_synthetic_watchdog(self):
        self._record("HEARTBEAT_ONLY_SILENCE")


if __name__ == "__main__":
    unittest.main()
