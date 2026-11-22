"""Actual existing Node collector archival byte parity with synthetic WS input.

Production source admission is not claimed. All input is locally generated,
Node marks the archive synthetic_fixture=true and ready_for_research=false.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from v36_live_engine import LiveMarketEngine

ROOT = Path(__file__).resolve().parents[1]


class ArchivedTapContract(unittest.TestCase):
    def test_tap_exposes_same_bytes_as_successfully_written_raw_archive(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("Node not available")
        with tempfile.TemporaryDirectory(prefix="v36_synthetic_tap_") as folder:
            work = Path(folder)
            sockpath = work / "tap.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sockpath))
            listener.listen(1)
            listener.settimeout(5)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": "15",
                "SOAK_RAW_BUDGET_BYTES": "1000000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "SOAK_CHUNK_LIMIT_BYTES": "8000000",
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
                "V36_TAP_FIXTURE_MODE": "1",
                "V36_LIVE_TAP_SOCKET": str(sockpath),
            }
            collector = ROOT / "scripts/btc_eth_multiday_v20.mjs"
            mock = ROOT / "tests/v36_mock_coinbase_websocket.mjs"
            proc = subprocess.Popen(
                [node, "--import", str(mock), str(collector)],
                cwd=work,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                peer, _ = listener.accept()
                peer.settimeout(5)
                chunks = []
                buf = bytearray()
                while len(chunks) < 8:
                    block = peer.recv(65536)
                    if not block:
                        break
                    buf.extend(block)
                    while b"\n" in buf:
                        n = buf.index(b"\n")
                        chunks.append(bytes(buf[: n + 1]))
                        del buf[: n + 1]
                self.assertEqual(len(chunks), 8)
                # Node owns the archive, not the observer. SIGTERM finalizes it.
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=6)
                archives = list(
                    (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
                )
                self.assertEqual(len(archives), 1)
                captured = archives[0]
                manifest = json.loads((captured / "manifest.json").read_bytes())
                self.assertTrue(manifest["synthetic_fixture"])
                self.assertFalse(manifest["ready_for_research"])
                self.assertEqual(manifest["messages"], 8)
                saved = b"".join(
                    (captured / "chunks" / "chunk_00000_conn_000.jsonl")
                    .open("rb")
                    .readlines()
                )
                self.assertEqual(saved, b"".join(chunks))
                state = LiveMarketEngine()  # OFFLINE fixture: NEVER LIVE_SOURCE
                for raw in chunks:
                    state.consume(raw)
                self.assertEqual(state.health()["status"], "HEALTHY")
                self.assertEqual(state.health()["mode"], "ARCHIVE_SIMULATION")
                self.assertIsNotNone(state.snapshot("BTC-USD")["state"])
                self.assertFalse(state.health()["live_feed"])
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
                listener.close()


if __name__ == "__main__":
    unittest.main()
