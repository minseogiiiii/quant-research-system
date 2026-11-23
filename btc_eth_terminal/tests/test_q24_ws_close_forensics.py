"""Q24 P0-A: redaction, malformed evidence, forced reconnect contracts.

Synthetic fixtures are never represented as real Coinbase source data.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from q24_ws_diagnostic_receipt import (  # noqa: E402
    Q24TraceRejected,
    build_receipt,
    read_private_trace,
)


def record(seq, event, **kwargs):
    row = {
        "schema": "q24_ws_close_event_v1",
        "seq": seq,
        "event": event,
        "connection_index": 0 if seq else None,
        "elapsed_ms": seq * 100,
        "connection_uptime_ms": seq * 100 if seq else None,
        "message_count": 1 if seq >= 2 else 0,
        "heartbeat_count": 1 if seq >= 2 else 0,
        "last_message_age_ms": 20 if seq >= 2 else None,
        "last_heartbeat_age_ms": 25 if seq >= 2 else None,
        "close_code": 1006 if event == "SOCKET_CLOSE" else None,
        "was_clean": False if event == "SOCKET_CLOSE" else None,
        "local_close_requested": False,
        "reconnect_delay_ms": None,
        "ping_pong_control_frames_visible": False,
        "real_orders_enabled": False,
    }
    row.update(kwargs)
    return row


class TraceSafety(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="q24_trace_")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.file = self.root / "private_trace.jsonl"

    def write(self, rows):
        self.file.write_text(
            "".join(json.dumps(x, sort_keys=True) + "\n" for x in rows)
        )
        os.chmod(self.file, 0o600)

    def clean(self):
        return [
            record(0, "TRACE_START"),
            record(1, "SOCKET_CONSTRUCTED"),
            record(2, "SOCKET_OPEN"),
            record(3, "SOCKET_ERROR"),
            record(4, "SOCKET_CLOSE"),
            record(5, "PROCESS_EXIT"),
        ]

    def test_abnormal_close_receipt_and_no_false_root_cause(self):
        self.write(self.clean())
        evidence = build_receipt(self.file)
        self.assertEqual(
            evidence["observed_close_class"],
            "WEBSOCKET_ERROR_PRECEDED_CLOSE_OBSERVED",
        )
        self.assertEqual(evidence["last_close_code"], 1006)
        self.assertEqual(evidence["opened_connections"], 1)
        self.assertEqual(evidence["last_close_heartbeat_age_ms"], 25)
        self.assertFalse(evidence["root_network_cause_proven"])
        self.assertFalse(
            evidence["native_ping_pong_control_frame_observation_available"]
        )
        self.assertFalse(evidence["real_orders_enabled"])

    def test_raw_secrets_and_unknown_keys_are_rejected(self):
        cases = [
            {"raw_text": "secret-price=777"},
            {"close_reason": "password"},
            {"error_message": "private.ip"},
            {"source_url": "wss://secret"},
        ]
        for mutation in cases:
            with self.subTest(extra=mutation):
                rows = self.clean()
                rows[4].update(mutation)
                self.write(rows)
                with self.assertRaises(Q24TraceRejected):
                    read_private_trace(self.file)

    def test_nonmonotone_clock_or_sequence_rejected(self):
        for field, value in (
            ("elapsed_ms", -1),
            ("seq", 999),
            ("close_code", 9000),
            ("local_close_requested", "false"),
            ("was_clean", "yes"),
            ("real_orders_enabled", True),
        ):
            with self.subTest(field=field):
                rows = self.clean()
                rows[4][field] = value
                self.write(rows)
                with self.assertRaises(Q24TraceRejected):
                    read_private_trace(self.file)

    def test_symlink_hardlink_and_world_readable_trace_rejected(self):
        rows = self.clean()
        self.write(rows)
        os.chmod(self.file, 0o644)
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(self.file)
        os.chmod(self.file, 0o600)
        second = self.root / "hardlink.jsonl"
        os.link(self.file, second)
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(self.file)
        second.unlink()
        alias = self.root / "symlink.jsonl"
        alias.symlink_to(self.file)
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(alias)

    def test_truncated_and_oversized_trace_rejected(self):
        self.write(self.clean())
        with self.file.open("ab") as stream:
            stream.write(b'{"partial":')
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(self.file)
        self.write([record(i, "PROGRESS") for i in range(481)])
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(self.file)


class CollectorProbeIntegration(unittest.TestCase):
    def test_actual_node_synthetic_forced_disconnect_writes_two_sockets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not installed")
        with tempfile.TemporaryDirectory(prefix="q24_gap_") as name:
            work = Path(name)
            private = work / "ws_close_private.jsonl"
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": "15",
                "SOAK_RAW_BUDGET_BYTES": "1000000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "SOAK_CHUNK_LIMIT_BYTES": "8000000",
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "6",
                "Q24_WS_DIAG_PRIVATE_PATH": str(private),
            }
            proc = subprocess.Popen(
                [
                    node,
                    "--import", str(ROOT / "tests/v36_mock_coinbase_websocket.mjs"),
                    "--import", str(ROOT / "scripts/q24_ws_close_probe.mjs"),
                    str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
                ],
                cwd=work,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    if private.exists():
                        try:
                            events = read_private_trace(private)
                        except Q24TraceRejected:
                            time.sleep(0.05)
                            continue
                        if sum(
                            row["event"] == "SOCKET_CONSTRUCTED"
                            for row in events
                        ) >= 2:
                            break
                    time.sleep(0.05)
                else:
                    self.fail("Synthetic reconnect was not observed")
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=8)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=4)
            receipt = build_receipt(private)
            self.assertGreaterEqual(receipt["observed_connections"], 2)
            self.assertGreaterEqual(receipt["close_events"], 1)
            self.assertGreaterEqual(receipt["local_close_requests"], 1)
            self.assertIsNotNone(receipt["last_reconnect_delay_ms"])
            self.assertFalse(receipt["root_network_cause_proven"])
            collector = next(
                (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
            )
            manifest = json.loads((collector / "manifest.json").read_text())
            self.assertTrue(manifest["synthetic_fixture"])
            self.assertFalse(manifest["ready_for_research"])
            self.assertGreaterEqual(len(manifest["connections"]), 2)


if __name__ == "__main__":
    unittest.main()
