"""Q25 P0-B: full-session quarantine with real unmodified Node collector.

Synthetic WebSocket stages make the previous BTC and ETH book visible,
inject a collector-reported transport gap and deliberately delay the new
ETH snapshot. No synthetic capture may be promoted to LIVE or research.
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
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from q25_session_isolation_guard import SourceEpochIsolatedEngine  # noqa: E402
from v36_live_gateway import server_for_live  # noqa: E402
from v36_observe_existing_collector import read_tap  # noqa: E402


def http_json(url, headers=None):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class GapWitness(SourceEpochIsolatedEngine):
    def __init__(self):
        super().__init__()
        self.first_ready = threading.Event()
        self.gap_seen = threading.Event()
        self.gap_health = None

    def _publish(self, product, receiver_ms, seq):
        super()._publish(product, receiver_ms, seq)
        if self.status == "HEALTHY" and self.connection_id == 0:
            self.first_ready.set()

    def transport_lost(self, reason="SOURCE_TRANSPORT_LOST"):
        with self._cv:
            super().transport_lost(reason)
            if reason == "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP":
                self.gap_health = self.health()
                self.gap_seen.set()


class Q25DirectErasure(unittest.TestCase):
    def test_gap_revokes_every_retained_market_buffer_atomically(self):
        e = SourceEpochIsolatedEngine()
        old_session = "a" * 32
        with e._cv:
            e.status = "HEALTHY"
            e.session_id = old_session
            for p in ("BTC-USD", "ETH-USD"):
                price = 100 if p == "BTC-USD" else 200
                e.books[p].apply("snapshot", [
                    {"side": "bid", "price_level": str(price),
                     "new_quantity": "5"},
                    {"side": "offer", "price_level": str(price + 1),
                     "new_quantity": "7"},
                ])
                e.rows[p].append({
                    "session_id": old_session, "product": p,
                    "state_version": 1, "best_bid": str(price),
                })
                e.ofi[p] = Decimal(75)
                e.last_l2[p] = (1800000000000, 5, "a" * 64, "11")
                e.last_pub[p] = 1800000000000
                e.recent_trades[p].append({"price": "SECRET_777777"})
                e.candles[p].append({"close": "SECRET_888888"})
                e.candle_open[p] = {"open": "SECRET_999999"}
        self.assertIsNotNone(e.snapshot("BTC-USD")["state"])
        e.transport_lost("COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP")
        h = e.health()
        self.assertEqual(h["status"], "RECONNECTING")
        self.assertEqual(h["q25_revocations_total"], 1)
        self.assertTrue(h["q25_quote_buffers_scrubbed"])
        self.assertEqual(h["q25_retained_rows_per_product"], {
            "BTC-USD": 0, "ETH-USD": 0,
        })
        for p in ("BTC-USD", "ETH-USD"):
            self.assertIsNone(e.snapshot(p)["state"])
            self.assertEqual(e.candles_page(p)["rows"], [])
            self.assertFalse(e.books[p].initialized)
            self.assertEqual(e.books[p].level["bid"], {})
            self.assertEqual(e.books[p].level["offer"], {})
            with self.assertRaisesRegex(ValueError, "RESYNC_REQUIRED"):
                e.stream_initial(p)
            with self.assertRaisesRegex(ValueError, "RESYNC_REQUIRED"):
                e.state_at_version(p, old_session, 1)
            with self.assertRaisesRegex(ValueError, "RESYNC_REQUIRED"):
                e.poll(p, old_session, 1, timeout=0.01)

    def test_stale_invalid_frame_and_stop_all_erase_before_public_read(self):
        for reason, phase in [
            ("HEARTBEAT_STALE", "STALE"),
            ("SOURCE_ENVELOPE_REJECTED:secret token", "QUARANTINED"),
            ("SOURCE_STOPPED", "STOPPED"),
        ]:
            with self.subTest(phase=phase):
                e = SourceEpochIsolatedEngine()
                with e._cv:
                    e.status = "HEALTHY"
                    e.session_id = "b" * 32
                    e.rows["BTC-USD"].append({
                        "session_id": "b" * 32, "product": "BTC-USD",
                        "state_version": 12, "best_bid": "SECRET",
                    })
                    e._invalid(reason, phase)
                h = e.health()
                self.assertTrue(h["q25_quote_buffers_scrubbed"])
                self.assertEqual(h["q25_revocations_total"], 1)
                self.assertNotIn("secret token", json.dumps(h.get(
                    "q25_last_revocation_class"
                )))
                self.assertIsNone(e.snapshot("BTC-USD")["state"])


class Q25FullCollectorGap(unittest.TestCase):
    def test_actual_collector_gap_does_not_expose_old_epoch_to_any_api(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        with tempfile.TemporaryDirectory(prefix="q25_gap_") as root:
            work = Path(root)
            tap = work / "tap.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tap))
            listener.listen(1)
            listener.settimeout(5)
            engine = GapWitness()
            server = server_for_live(engine)
            thread = threading.Thread(
                target=server.serve_forever, daemon=True
            )
            thread.start()
            host = "http://127.0.0.1:" + str(server.server_address[1])
            done = [False]
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": "15",
                "SOAK_RAW_BUDGET_BYTES": "1000000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "SOAK_CHUNK_LIMIT_BYTES": "8000000",
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "6",
                "V36_TAP_FIXTURE_MODE": "1",
                "V36_LIVE_TAP_SOCKET": str(tap),
            }
            proc = subprocess.Popen([
                node, "--import",
                str(ROOT / "tests/q25_staged_reconnect_coinbase_mock.mjs"),
                str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
            ], cwd=work, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            reader = None
            old_sse = None
            try:
                sock, _ = listener.accept()
                reader = threading.Thread(
                    target=read_tap, args=(sock, engine, done), daemon=True
                )
                reader.start()
                self.assertTrue(engine.first_ready.wait(3),
                                "First source must be valid before fault")
                initial = engine.snapshot("BTC-USD")["state"]
                self.assertIsNotNone(initial)
                old_session = initial["session_id"]
                old_version = initial["state_version"]
                self.assertEqual(
                    http_json(host + "/api/v36/snapshot?product=ETH-USD")[0],
                    200,
                )
                old_sse = urlopen(
                    host + "/api/v36/stream?product=BTC-USD", timeout=3
                )
                self.assertEqual(old_sse.status, 200)
                self.assertTrue(engine.gap_seen.wait(4))
                # The gap event is captured under the engine lock before
                # the next connection is permitted to repopulate anything.
                self.assertTrue(
                    engine.gap_health["q25_quote_buffers_scrubbed"]
                )
                self.assertEqual(
                    engine.gap_health["q25_retained_rows_per_product"],
                    {"BTC-USD": 0, "ETH-USD": 0},
                )
                self.assertEqual(engine.gap_health["status"], "RECONNECTING")
                self.assertEqual(
                    engine.gap_health["q25_last_revocation_class"],
                    "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP",
                )
                self.assertTrue(wait_until(
                    lambda: engine.health()["connection_id"] == 1
                    and engine.health()["snapshots"] == ["BTC-USD"]
                    and engine.health()["status"] == "WAITING_FOR_SNAPSHOT",
                    timeout=3,
                ), "BTC-only partial second connection was not observed")
                partial = engine.health()
                self.assertNotEqual(partial["session_id"], old_session)
                self.assertEqual(partial["connection_gaps"], 1)
                for product in ("BTC-USD", "ETH-USD"):
                    path = "/api/v36/snapshot?product=" + product
                    code, obj = http_json(host + path)
                    self.assertEqual(code, 503)
                    self.assertIsNone(obj["state"])
                    self.assertEqual(
                        http_json(host + "/api/v36/candles?product=" +
                                  product)[1]["rows"], []
                    )
                state_path = (
                    "/api/v36/state?product=BTC-USD&session=" +
                    old_session + "&version=" + str(old_version)
                )
                self.assertEqual(http_json(host + state_path)[0], 409)
                self.assertEqual(
                    http_json(host + "/api/v36/stream?product=BTC-USD")[0],
                    409,
                )
                self.assertTrue(wait_until(
                    lambda: engine.health()["connection_id"] == 1
                    and engine.health()["status"] == "HEALTHY",
                    timeout=4,
                ), "New dual snapshot/acks/heartbeat did not restore source")
                h = engine.health()
                self.assertEqual(h["snapshots"], ["BTC-USD", "ETH-USD"])
                self.assertEqual(
                    h["subscriptions"],
                    ["heartbeats", "level2", "market_trades"],
                )
                self.assertGreater(h["heartbeats"], 0)
                self.assertNotEqual(old_session, h["session_id"])
                self.assertEqual(h["q25_revocations_total"], 1)
                for product in ("BTC-USD", "ETH-USD"):
                    fresh = http_json(
                        host + "/api/v36/snapshot?product=" + product
                    )
                    self.assertEqual(fresh[0], 200)
                    self.assertEqual(
                        fresh[1]["state"]["session_id"], h["session_id"]
                    )
                    self.assertEqual(fresh[1]["state"]["connection_id"], 1)
                    # Never assert OFI remains zero after a valid new-
                    # session L2 update. It must be recalculated from this
                    # connection only, and the old quote must be absent.
                    self.assertIn(
                        "ofi_since_connection_base", fresh[1]["state"]
                    )
                self.assertEqual(http_json(host + state_path)[0], 409)
                old_cursor = (
                    "v36:" + old_session + ":BTC-USD:" + str(old_version)
                )
                self.assertEqual(
                    http_json(
                        host + "/api/v36/stream?product=BTC-USD",
                        {"Last-Event-ID": old_cursor},
                    )[0], 409,
                )
                # Previously opened SSE should receive a resync_required
                # event instead of continuing to stream a different epoch.
                wire = bytearray()
                for _ in range(32):
                    line = old_sse.readline()
                    if not line:
                        break
                    wire.extend(line)
                    if b"event: resync_required" in wire:
                        break
                self.assertIn(b"event: resync_required", wire)
                self.assertEqual(
                    http_json(host + "/api/v36/health")[1]["q25_orders_enabled"],
                    False,
                )
            finally:
                if old_sse is not None:
                    old_sse.close()
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=8)
                done[0] = True
                if reader is not None:
                    reader.join(timeout=4)
                server.shutdown()
                server.server_close()
                thread.join(timeout=4)
                listener.close()
            source = next((work / "soak_evidence").glob(
                "coinbase_btc_eth_soak_*"
            ))
            manifest = json.loads((source / "manifest.json").read_text())
            self.assertTrue(manifest["synthetic_fixture"])
            self.assertFalse(manifest["ready_for_research"])
            self.assertGreaterEqual(len(manifest["connections"]), 2)
            self.assertGreaterEqual(len(manifest["faults"]), 1)
            self.assertTrue(all(
                chunk["connection_id"] in (0, 1)
                for chunk in manifest["chunks"]
            ))


if __name__ == "__main__":
    unittest.main()
