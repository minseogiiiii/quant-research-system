"""Synthetic-only multi-chunk and SIGKILL tests for the isolated durable gate."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v36_3_durable_archive_gate import DurableRejected, audit


def fixture(root, limit=520):
    (root / "chunks").mkdir()
    started = datetime(2026, 11, 1, tzinfo=UTC)
    meta = {
        "format": "btc_eth_raw_soak_v10",
        "source": "wss://advanced-trade-ws.coinbase.com",
        "source_authentication": "PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        "products": ["BTC-USD", "ETH-USD"],
        "subscribed_channels": ["level2", "market_trades", "heartbeats"],
        "orders_enabled": False,
        "strategy_execution_enabled": False,
        "point_in_time_historical_availability_verified": False,
        "executable_alpha_verified": False,
        "sequence_scope": "PER_WEBSOCKET_CONNECTION",
        "synthetic_fixture": True,
        "controlled_fault_injection": None,
        "required_duration_seconds": 15,
        "status": "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "ready_for_research": True,
        "finish_reason": "DURATION_REACHED",
        "complete_sequence_per_connection": True,
        "all_connections_have_snapshots": True,
        "total_reconnects": 0,
        "faults": [],
        "chunks": [],
        "connections": [
            {
                "id": 0,
                "messages": limit,
                "last_sequence": limit - 1,
                "snapshots": {"BTC-USD": True, "ETH-USD": True},
                "integrity_fault": False,
            }
        ],
        "messages": limit,
        "bytes": 0,
    }
    for n in range(limit):
        if n < 2:
            product = ("BTC-USD", "ETH-USD")[n]
            channel = "l2_data"
            events = [
                {
                    "type": "snapshot",
                    "product_id": product,
                    "updates": [
                        {"side": "bid", "price_level": "100", "new_quantity": "1"}
                    ],
                }
            ]
        elif n == 2:
            channel = "heartbeats"
            events = [{"heartbeat_counter": 1}]
        else:
            channel = "l2_data"
            product = "BTC-USD" if n % 2 else "ETH-USD"
            events = [
                {
                    "type": "update",
                    "product_id": product,
                    "updates": [
                        {"side": "bid", "price_level": "100", "new_quantity": "2"}
                    ],
                }
            ]
        wire = json.dumps(
            {"channel": channel, "events": events, "sequence_num": n},
            separators=(",", ":"),
        )
        recv = (
            (started + timedelta(milliseconds=20 * n))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        row = (
            json.dumps(
                {
                    "connection_id": 0,
                    "received_utc": recv,
                    "local_monotonic_ns": str(1_000_000_000 + 20_000_000 * n),
                    "raw_text": wire,
                    "sha256": hashlib.sha256(wire.encode()).hexdigest(),
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        name = f"chunk_{n:05d}_conn_000.jsonl"
        (root / "chunks" / name).write_bytes(row)
        meta["chunks"].append(
            {
                "file": "chunks/" + name,
                "connection_id": 0,
                "messages": 1,
                "raw_text_bytes": len(wire.encode()),
                "archived_line_bytes": len(row),
                "first_sequence": n,
                "last_sequence": n,
                "first_received_utc": recv,
                "last_received_utc": recv,
                "archive_sha256": hashlib.sha256(row).hexdigest(),
            }
        )
        meta["bytes"] += len(wire.encode())
    raw = (json.dumps(meta, indent=2) + "\n").encode()
    (root / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class DurableForensicSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v36_3_source_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.root.mkdir()
        self.pin = fixture(self.root)

    def test_more_than_512_chunks_are_verified_but_synthetic_never_admitted(self):
        self.assertGreater(520, 512)
        r = audit(self.root, self.pin)
        self.assertEqual(r["sealed_chunks_verified"], 520)
        self.assertEqual(r["sealed_messages_verified"], 520)
        self.assertFalse(r["eligible_for_archived_replay"])
        self.assertFalse(r["actual_persistent_24h_host_certified"])

    def test_changed_chunk_rejected_even_when_manifest_pin_matches(self):
        p = self.root / "chunks/chunk_00010_conn_000.jsonl"
        b = bytearray(p.read_bytes())
        b[-20] ^= 1
        p.write_bytes(b)
        with self.assertRaises(DurableRejected):
            audit(self.root, self.pin)

    def test_unknown_chunk_and_symlink_rejected(self):
        p = self.root / "chunks/unlisted.jsonl"
        p.write_bytes(b"unknown\n")
        with self.assertRaisesRegex(DurableRejected, "UNLISTED_FINALIZED"):
            audit(self.root, self.pin)
        p.unlink()
        q = self.root / "chunks/chunk_00003_conn_000.jsonl"
        q.unlink()
        q.symlink_to(self.root / "manifest.json")
        with self.assertRaises(DurableRejected):
            audit(self.root, self.pin)

    def test_hardlinked_chunk_is_rejected(self):
        original = self.root / "chunks/chunk_00010_conn_000.jsonl"
        external = Path(self.tmp.name) / "external-source-hardlink"
        os.link(original, external)
        with self.assertRaisesRegex(DurableRejected, "UNSAFE_LINKED"):
            audit(self.root, self.pin)

    def test_hardlinked_manifest_is_rejected(self):
        external = Path(self.tmp.name) / "external-manifest-hardlink"
        os.link(self.root / "manifest.json", external)
        with self.assertRaisesRegex(DurableRejected, "UNSAFE_LINKED"):
            audit(self.root, self.pin)

    def test_partial_and_manifest_temp_can_never_promote(self):
        (self.root / "chunks/chunk_00520_conn_000.jsonl.partial").write_bytes(
            b"partial"
        )
        (self.root / "manifest.json.tmp").write_bytes(b"uncommitted")
        result = audit(self.root, self.pin)
        self.assertFalse(result["eligible_for_archived_replay"])
        self.assertEqual(result["partial_chunks_not_admitted"], 1)
        self.assertTrue(result["manifest_tmp_not_admitted"])

    def test_forged_day_duration_can_never_certify(self):
        m = json.loads((self.root / "manifest.json").read_bytes())
        m["synthetic_fixture"] = False
        m["required_duration_seconds"] = 86400
        raw = json.dumps(m).encode()
        (self.root / "manifest.json").write_bytes(raw)
        r = audit(self.root, hashlib.sha256(raw).hexdigest())
        self.assertFalse(r["eligible_for_archived_replay"])
        self.assertFalse(r["actual_persistent_24h_host_certified"])

    def test_manifest_pin_mismatch_rejected(self):
        with self.assertRaisesRegex(DurableRejected, "MANIFEST_PIN_MISMATCH"):
            audit(self.root, "0" * 64)

    def test_actual_node_sigkill_preserves_partial_and_new_start_is_separate(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 22+ unavailable")
        source = ROOT / "scripts/btc_eth_multiday_v20.mjs"
        mock = ROOT / "tests/v36_mock_coinbase_websocket.mjs"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SOAK_DURATION_SECONDS": "15",
            "SOAK_RAW_BUDGET_BYTES": "1000000000",
            "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
            "SOAK_DISK_RESERVE_BYTES": "0",
            "SOAK_CHUNK_LIMIT_BYTES": "8000000",
            "SOAK_TEST_FIXTURE": "1",
            "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
        }
        a = Path(self.tmp.name) / "run_a"
        a.mkdir()
        p = subprocess.Popen(
            [node, "--import", str(mock), str(source)],
            env=env,
            cwd=a,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            end = time.monotonic() + 6
            while time.monotonic() < end:
                parts = list(
                    a.glob("soak_evidence/coinbase_btc_eth_soak_*/chunks/*.partial")
                )
                if parts and parts[0].stat().st_size > 500:
                    break
                time.sleep(0.02)
            else:
                self.fail("No actual active Node archive .partial")
            p.kill()
            p.communicate(timeout=3)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=3)
        originals = list(a.glob("soak_evidence/coinbase_btc_eth_soak_*"))
        self.assertEqual(len(originals), 1)
        before = {
            str(f.relative_to(originals[0])): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in originals[0].rglob("*")
            if f.is_file()
        }
        manifest = originals[0] / "manifest.json"
        result = audit(originals[0], hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.assertFalse(result["eligible_for_archived_replay"])
        self.assertGreaterEqual(result["partial_chunks_not_admitted"], 1)
        b = Path(self.tmp.name) / "run_b"
        b.mkdir()
        q = subprocess.Popen(
            [node, "--import", str(mock), str(source)],
            env=env,
            cwd=b,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(0.6)
            q.send_signal(signal.SIGTERM)
            q.communicate(timeout=7)
        finally:
            if q.poll() is None:
                q.kill()
                q.communicate(timeout=3)
        self.assertEqual(len(list(b.glob("soak_evidence/coinbase_btc_eth_soak_*"))), 1)
        after = {
            str(f.relative_to(originals[0])): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in originals[0].rglob("*")
            if f.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
