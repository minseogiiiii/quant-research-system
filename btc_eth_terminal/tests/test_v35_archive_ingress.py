"""Reproducible source-integrity tests with independent real-archive controls."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from v35_archive_ingress import ArchiveRejected, verify_capture


def _json_bytes(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def _fixture(directory, *, disconnect=False, mutate=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chunks").mkdir()
    sequences = [0, 1, 2, 3]
    channels = [
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "70000", "new_quantity": "1"}
                    ],
                }
            ],
        },
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "ETH-USD",
                    "updates": [
                        {"side": "offer", "price_level": "2000", "new_quantity": "2"}
                    ],
                }
            ],
        },
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": "update",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "70000", "new_quantity": "2"}
                    ],
                }
            ],
        },
        {"channel": "market_trades", "events": [{"type": "update", "trades": []}]},
    ]
    envelopes = []
    for i, event in enumerate(channels):
        wire = {"sequence_num": sequences[i], **event}
        raw = json.dumps(wire, separators=(",", ":"))
        envelopes.append(
            {
                "connection_id": 0,
                "received_utc": f"2026-11-14T07:26:19.{i:03}Z",
                "local_monotonic_ns": str(1_000_000 + i * 100_000),
                "raw_text": raw,
                "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
        )
    if mutate is not None:
        mutate(envelopes)
    blob = b"".join(_json_bytes(e) for e in envelopes)
    filename = "chunks/chunk_00000_conn_000.jsonl"
    (directory / filename).write_bytes(blob)
    metadata = {
        "format": "btc_eth_raw_soak_v10",
        "source": "wss://advanced-trade-ws.coinbase.com",
        "source_authentication": "PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        "products": ["BTC-USD", "ETH-USD"],
        "sequence_scope": "PER_WEBSOCKET_CONNECTION",
        "orders_enabled": False,
        "strategy_execution_enabled": False,
        "point_in_time_historical_availability_verified": False,
        "executable_alpha_verified": False,
        "ready_for_research": not disconnect,
        "status": (
            "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
            if disconnect
            else "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        ),
        "complete_sequence_per_connection": not disconnect,
        "all_connections_have_snapshots": True,
        "faults": (
            [{"reason": "INJECTED_DISCONNECT_AFTER_COMMITTED_SOURCE"}]
            if disconnect
            else []
        ),
        "chunks": [
            {
                "file": filename,
                "connection_id": 0,
                "messages": len(envelopes),
                "archived_line_bytes": len(blob),
                "raw_text_bytes": sum(len(e["raw_text"].encode()) for e in envelopes),
                "archive_sha256": hashlib.sha256(blob).hexdigest(),
                "first_sequence": 0,
                "last_sequence": len(envelopes) - 1,
                "first_received_utc": envelopes[0]["received_utc"],
                "last_received_utc": envelopes[-1]["received_utc"],
            }
        ],
        "connections": [
            {
                "id": 0,
                "messages": len(envelopes),
                "last_sequence": len(envelopes) - 1,
                "snapshots": {"BTC-USD": True, "ETH-USD": True},
                "integrity_fault": disconnect,
            }
        ],
        "messages": len(envelopes),
        "bytes": sum(len(e["raw_text"].encode()) for e in envelopes),
    }
    (directory / "manifest.json").write_bytes(_json_bytes(metadata))
    return hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()


class IngressContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "source"
        self.pin = _fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _rewrite(self, operation):
        """Re-pin malicious fixture to prove structural checks operate after SHA verification."""
        envelope_file = next((self.root / "chunks").glob("*.jsonl"))
        messages = [json.loads(s) for s in envelope_file.read_bytes().splitlines()]
        operation(messages)
        for entry in messages:
            entry["sha256"] = hashlib.sha256(entry["raw_text"].encode()).hexdigest()
        blob = b"".join(_json_bytes(e) for e in messages)
        envelope_file.write_bytes(blob)
        meta = json.loads((self.root / "manifest.json").read_bytes())
        chunk = meta["chunks"][0]
        chunk["archive_sha256"] = hashlib.sha256(blob).hexdigest()
        chunk["archived_line_bytes"] = len(blob)
        chunk["raw_text_bytes"] = sum(len(e["raw_text"].encode()) for e in messages)
        meta["bytes"] = chunk["raw_text_bytes"]
        chunk["first_received_utc"] = messages[0]["received_utc"]
        chunk["last_received_utc"] = messages[-1]["received_utc"]
        (self.root / "manifest.json").write_bytes(_json_bytes(meta))
        self.pin = hashlib.sha256(
            (self.root / "manifest.json").read_bytes()
        ).hexdigest()

    def test_clean_fixture_is_archive_only(self):
        report = verify_capture(self.root, self.pin)
        self.assertEqual(report["admission"], "VERIFIED_ARCHIVE_ENVELOPE")
        self.assertEqual(report["mode"], "ARCHIVE_REPLAY")
        for k in (
            "live_feed",
            "execution_authorized",
            "trading_enabled",
            "oos_approved",
        ):
            self.assertIs(report[k], False)

    def test_external_manifest_pin_is_required(self):
        for bad in ("", "bad", "0" * 64):
            with self.assertRaises(ArchiveRejected):
                verify_capture(self.root, bad)

    def test_corrupted_archive_chunk_fails(self):
        file = next((self.root / "chunks").glob("*.jsonl"))
        file.write_bytes(file.read_bytes().replace(b"70000", b"70001", 1))
        with self.assertRaisesRegex(ArchiveRejected, "CHUNK_HASH_OR_LENGTH_MISMATCH"):
            verify_capture(self.root, self.pin)

    def test_rehash_cannot_hide_wire_sequence_gap(self):
        def break_seq(data):
            raw = json.loads(data[2]["raw_text"])
            raw["sequence_num"] = 42
            data[2]["raw_text"] = json.dumps(raw)

        self._rewrite(break_seq)
        with self.assertRaisesRegex(ArchiveRejected, "WIRE_SEQUENCE_GAP_OR_REORDER"):
            verify_capture(self.root, self.pin)

    def test_rehash_cannot_hide_snapshot_before_delta_failure(self):
        def order(data):
            raw = json.loads(data[0]["raw_text"])
            raw["events"][0]["type"] = "update"
            data[0]["raw_text"] = json.dumps(raw)

        self._rewrite(order)
        with self.assertRaisesRegex(
            ArchiveRejected, "UPDATE_BEFORE_CONNECTION_SNAPSHOT"
        ):
            verify_capture(self.root, self.pin)

    def test_rehash_cannot_hide_receiver_time_regression(self):
        self._rewrite(
            lambda data: data[2].update(received_utc="2026-11-14T07:26:18.000Z")
        )
        with self.assertRaisesRegex(
            ArchiveRejected, "RECEIVER_ORDER_OR_MONOTONIC_REGRESSION"
        ):
            verify_capture(self.root, self.pin)

    def test_rehash_cannot_hide_monotonic_regression(self):
        self._rewrite(lambda data: data[2].update(local_monotonic_ns="1"))
        with self.assertRaisesRegex(
            ArchiveRejected, "RECEIVER_ORDER_OR_MONOTONIC_REGRESSION"
        ):
            verify_capture(self.root, self.pin)

    def test_rehash_cannot_hide_cross_product_message(self):
        def change_product(data):
            raw = json.loads(data[0]["raw_text"])
            raw["events"][0]["product_id"] = "DOGE-USD"
            data[0]["raw_text"] = json.dumps(raw)

        self._rewrite(change_product)
        with self.assertRaisesRegex(ArchiveRejected, "CROSS_PRODUCT_L2_EVENT"):
            verify_capture(self.root, self.pin)

    def test_path_traversal_in_manifest_rejected(self):
        meta = json.loads((self.root / "manifest.json").read_bytes())
        meta["chunks"][0]["file"] = "../escape.jsonl"
        (self.root / "manifest.json").write_bytes(_json_bytes(meta))
        pin = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(ArchiveRejected, "UNSAFE_CHUNK_NAME"):
            verify_capture(self.root, pin)

    def test_repinned_noninteger_connection_id_fails_closed(self):
        meta = json.loads((self.root / "manifest.json").read_bytes())
        meta["chunks"][0]["connection_id"] = "0"
        (self.root / "manifest.json").write_bytes(_json_bytes(meta))
        pin = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            ArchiveRejected, "CONNECTION_BOUNDARY_OUT_OF_ORDER"
        ):
            verify_capture(self.root, pin)

    def test_repinned_malformed_product_inventory_fails_closed(self):
        meta = json.loads((self.root / "manifest.json").read_bytes())
        meta["products"] = [{"product_id": "BTC-USD"}, "ETH-USD"]
        (self.root / "manifest.json").write_bytes(_json_bytes(meta))
        pin = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(ArchiveRejected, "MALFORMED_PRODUCT_INVENTORY"):
            verify_capture(self.root, pin)

    def test_symlinked_archive_chunk_rejected(self):
        file = next((self.root / "chunks").glob("*.jsonl"))
        real = file.with_suffix(".backup")
        file.rename(real)
        file.symlink_to(real)
        with self.assertRaisesRegex(ArchiveRejected, "MISSING_OR_SYMLINKED_EVIDENCE"):
            verify_capture(self.root, self.pin)

    def test_injected_fault_is_quarantined_not_promoted(self):
        shutil.rmtree(self.root)
        self.pin = _fixture(self.root, disconnect=True)
        report = verify_capture(self.root, self.pin)
        self.assertEqual(report["admission"], "QUARANTINED_ARCHIVE_ENVELOPE")
        self.assertFalse(report["source_eligible_for_state_replay"])

    def test_manifest_permission_escalation_fails_closed(self):
        meta = json.loads((self.root / "manifest.json").read_bytes())
        meta["orders_enabled"] = True
        (self.root / "manifest.json").write_bytes(_json_bytes(meta))
        pin = hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            ArchiveRejected, "UNSUPPORTED_SOURCE_OR_TRADING_PERMISSIONS"
        ):
            verify_capture(self.root, pin)


class FrozenRealSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean = os.environ.get("V35_CLEAN_ARCHIVE")
        fault = os.environ.get("V35_DISCONNECT_ARCHIVE")
        if not clean or not fault:
            raise unittest.SkipTest(
                "Requires path to independently frozen 120s Coinbase source captures"
            )
        cls.clean = Path(clean)
        cls.fault = Path(fault)

    def test_actual_clean_archive_is_eligible_only_for_replay(self):
        result = verify_capture(
            self.clean,
            "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5",
        )
        self.assertEqual(result["messages"], 5177)
        self.assertEqual(result["chunks"], 28)
        self.assertEqual(result["admission"], "VERIFIED_ARCHIVE_ENVELOPE")
        self.assertFalse(result["live_feed"])

    def test_actual_disconnect_archive_quarantined(self):
        result = verify_capture(
            self.fault,
            "35e024fc87b317ebcf06c452562ef809a996bcc350c581356b7394c8022e5955",
        )
        self.assertEqual(result["messages"], 5164)
        self.assertEqual(result["connections"], 2)
        self.assertEqual(result["admission"], "QUARANTINED_ARCHIVE_ENVELOPE")
        self.assertFalse(result["source_eligible_for_state_replay"])


if __name__ == "__main__":
    unittest.main()
