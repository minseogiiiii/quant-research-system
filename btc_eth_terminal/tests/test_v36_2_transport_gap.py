"""Synthetic real collector/tap gap-injection; never claims genuine exchange data.

The unmodified archive writer's fault-injection reconnect must immediately
revoke the previous terminal session; a later fresh snapshot grants a DIFFERENT
SSE session and retains forensic archived connection boundaries. No order path.
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
sys.path.insert(0, str(ROOT / "backend"))
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live

sys.path.insert(0, str(ROOT / "scripts"))
from v36_observe_existing_collector import read_tap


class ObservedEngine(LiveMarketEngine):
    def __init__(self):
        super().__init__(mode="ARCHIVE_SIMULATION")
        self.first_session = None
        self.fault_session = None
        self.fault_state = None
        self.old_session_revoked = False
        self.gap_seen = threading.Event()

    def _publish(self, product, received, seq):
        super()._publish(product, received, seq)
        if self.status == "HEALTHY" and self.first_session is None:
            self.first_session = self.session_id

    def transport_lost(self, reason="SOURCE_TRANSPORT_LOST"):
        with self._cv:
            if "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP" in reason:
                self.fault_session = self.session_id
                super().transport_lost(reason)
                self.old_session_revoked = (
                    self.snapshot("BTC-USD")["state"] is None
                    and self.snapshot("ETH-USD")["state"] is None
                )
                self.fault_state = self.status
                self.gap_seen.set()
                return
        return super().transport_lost(reason)


class CollectorLiveGap(unittest.TestCase):
    def test_forced_disconnect_and_fresh_dual_snapshot_never_stitches_session(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        with tempfile.TemporaryDirectory(prefix="v36_gap_fault_") as folder:
            work = Path(folder)
            tap = work / "tap.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tap))
            listener.listen(1)
            listener.settimeout(6)
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
            engine = ObservedEngine()
            done = [False]
            server = server_for_live(engine)
            http = threading.Thread(target=server.serve_forever, daemon=True)
            http.start()
            proc = subprocess.Popen(
                [
                    node,
                    "--import",
                    str(ROOT / "tests/v36_mock_coinbase_websocket.mjs"),
                    str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
                ],
                cwd=work,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            reader = None
            try:
                sock, _ = listener.accept()
                reader = threading.Thread(
                    target=read_tap, args=(sock, engine, done), daemon=True
                )
                reader.start()
                self.assertTrue(
                    engine.gap_seen.wait(5),
                    "Collector did not emit immediate source gap",
                )
                self.assertTrue(engine.old_session_revoked)
                self.assertEqual(engine.fault_state, "RECONNECTING")
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    h = engine.health()
                    if h["status"] == "HEALTHY" and h["connection_id"] == 1:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(
                        "Second authenticated source snapshot did not reinitialize engine"
                    )
                self.assertEqual(engine.first_session, engine.fault_session)
                self.assertNotEqual(engine.first_session, h["session_id"])
                self.assertEqual(h["connection_gaps"], 1)
                self.assertEqual(h["snapshots"], ["BTC-USD", "ETH-USD"])
                self.assertEqual(
                    h["subscriptions"], ["heartbeats", "level2", "market_trades"]
                )
                self.assertIsNotNone(engine.snapshot("BTC-USD")["state"])
                self.assertIsNotNone(engine.snapshot("ETH-USD")["state"])
                old = f"v36:{engine.first_session}:BTC-USD:1"
                req = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/v36/stream?product=BTC-USD",
                    headers={"Last-Event-ID": old},
                )
                with self.assertRaises(HTTPError) as cm:
                    urlopen(req, timeout=3)
                self.assertEqual(cm.exception.code, 409)
                self.assertIn("RESYNC_REQUIRED", cm.exception.read().decode())
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                out, err = proc.communicate(timeout=7)
                done[0] = True
                if reader:
                    reader.join(timeout=4)
                server.shutdown()
                server.server_close()
                http.join(timeout=4)
                listener.close()
            archive_dirs = list(
                (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
            )
            self.assertEqual(len(archive_dirs), 1)
            manifest = json.loads((archive_dirs[0] / "manifest.json").read_bytes())
            self.assertFalse(manifest["ready_for_research"])
            self.assertTrue(manifest["synthetic_fixture"])
            self.assertGreaterEqual(len(manifest["connections"]), 2)
            self.assertGreaterEqual(len(manifest["faults"]), 1)
            self.assertGreaterEqual(
                len(list((archive_dirs[0] / "chunks").glob("*.jsonl"))), 2
            )


if __name__ == "__main__":
    unittest.main()
