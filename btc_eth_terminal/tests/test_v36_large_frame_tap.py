"""Regression: Node's large accepted write must not kill live observer tap.

No public network, credentials, or actual Coinbase records. The first message
is a 4.7 MiB SYNTHETIC l2 snapshot with harmless padding, which reproduces
a real archived-first-snapshot-size boundary without publishing market data.
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

ROOT = Path(__file__).resolve().parents[1]


class LargeFrameObserverTap(unittest.TestCase):
    def test_first_4_7mb_snapshot_is_delivered_byte_identically(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 22+ unavailable")
        with tempfile.TemporaryDirectory(prefix="v36_big_tap_") as folder:
            work = Path(folder)
            sockpath = work / "tap.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sockpath))
            listener.listen(1)
            listener.settimeout(10)
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
                "V36_TAP_LARGE_SNAPSHOT_TEST": "1",
            }
            mock = ROOT / "tests/v36_mock_coinbase_websocket.mjs"
            collector = ROOT / "scripts/btc_eth_multiday_v20.mjs"
            proc = subprocess.Popen(
                [node, "--import", str(mock), str(collector)],
                cwd=work,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                peer, _ = listener.accept()
                peer.settimeout(10)
                frames = []
                buffer = bytearray()
                with peer:
                    while len(frames) < 8:
                        piece = peer.recv(524288)
                        if not piece:
                            break
                        buffer.extend(piece)
                        while True:
                            newline = buffer.find(b"\n")
                            if newline < 0:
                                break
                            frames.append(bytes(buffer[: newline + 1]))
                            del buffer[: newline + 1]
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(len(frames), 8, stderr.decode(errors="replace")[-800:])
                self.assertGreater(len(frames[0]), 4_700_000)
                locations = list(
                    (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
                )
                self.assertEqual(len(locations), 1)
                source = locations[0]
                manifest = json.loads((source / "manifest.json").read_bytes())
                self.assertTrue(manifest["synthetic_fixture"])
                self.assertFalse(manifest["ready_for_research"])
                self.assertEqual(manifest["messages"], 8)
                chunk_files = sorted((source / "chunks").glob("*.jsonl"))
                self.assertGreaterEqual(len(chunk_files), 1)
                self.assertEqual(
                    b"".join(f.read_bytes() for f in chunk_files), b"".join(frames)
                )
                self.assertEqual(
                    [
                        json.loads(f)["raw_text"]
                        and json.loads(json.loads(f)["raw_text"])["sequence_num"]
                        for f in frames
                    ],
                    list(range(8)),
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)
                listener.close()


if __name__ == "__main__":
    unittest.main()
