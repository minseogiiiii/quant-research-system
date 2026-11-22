"""Synthetic contract tests only: no claim of real 24h Linux certification."""

import copy
import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
from q21_host_witness import (  # noqa: E402
    DISK_RESERVE_BYTES,
    SCHEMA,
    WitnessRejected,
    audit_witness,
    capture_sample,
    verify_sequence,
)

HEX = "a" * 64


def sample(i=0):
    return {
        "schema": SCHEMA,
        "index": i,
        "wall_time_ns": 1_800_000_000_000_000_000 + i * 60_000_000_000,
        "monotonic_ns": 400_000_000_000 + i * 60_000_000_000,
        "boot_id_sha256": HEX,
        "observer_pid": 4321,
        "source_session_sha256": "b" * 64,
        "messages": 10_000 + 10 * i,
        "last_sequence": 9_999 + 10 * i,
        "heartbeats": 100 + i,
        "connection_gaps": 0,
        "source_age_ms": 200,
        "active_sse_connections": 4,
        "observer_rss_bytes": 300_000_000,
        "observer_open_fds": 100,
        "disk_free_bytes": DISK_RESERVE_BYTES + 100_000_000,
        "trading_enabled": False,
        "synthetic_data": False,
    }


class Q21WitnessContract(unittest.TestCase):
    def test_two_valid_samples_are_only_a_synthetic_witness(self):
        result = verify_sequence([sample(0), sample(1)], minimum_span=60)
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["observed_seconds"], 60)
        self.assertFalse(result["persistent_host_24h_certified"])
        self.assertFalse(result["independent_operator_approval"])

    def test_fail_closed_single_source_and_single_boot(self):
        for change in (
            {"source_session_sha256": "c" * 64},
            {"boot_id_sha256": "d" * 64},
            {"observer_pid": 99},
            {"trading_enabled": True},
            {"synthetic_data": True},
            {"connection_gaps": 1},
            {"source_age_ms": 15001},
            {"active_sse_connections": 9},
            {"observer_open_fds": 4097},
            {"observer_rss_bytes": 3 * 1024**3},
            {"disk_free_bytes": DISK_RESERVE_BYTES - 1},
        ):
            rows = [sample(0), {**sample(1), **change}]
            with self.subTest(change=change):
                with self.assertRaises(WitnessRejected):
                    verify_sequence(rows, minimum_span=60)

    def test_no_missing_or_duplicated_sample_index(self):
        with self.assertRaises(WitnessRejected):
            verify_sequence([sample(0), sample(2)], minimum_span=60)

    def test_clock_jump_and_monotonic_gap(self):
        for change in (
            {"monotonic_ns": sample(0)["monotonic_ns"] - 1},
            {"monotonic_ns": sample(0)["monotonic_ns"] + 121_000_000_000},
            {"wall_time_ns": sample(0)["wall_time_ns"] + 10_000_000_000},
        ):
            with self.subTest(change=change):
                with self.assertRaises(WitnessRejected):
                    verify_sequence([sample(0), {**sample(1), **change}],
                                    minimum_span=60)

    def test_stale_or_decreasing_source_progress(self):
        for change in (
            {"messages": sample(0)["messages"]},
            {"last_sequence": sample(0)["last_sequence"]},
            {"heartbeats": sample(0)["heartbeats"] - 1},
            {"messages": True},
        ):
            with self.subTest(change=change):
                with self.assertRaises(WitnessRejected):
                    verify_sequence([sample(0), {**sample(1), **change}],
                                    minimum_span=60)

    def test_short_duration_is_not_24_hour_evidence(self):
        with self.assertRaisesRegex(WitnessRejected, "DURATION"):
            verify_sequence([sample(0), sample(1)])

    def test_rejects_mismatched_external_witness_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "witness.jsonl"
            path.write_text(json.dumps(sample()) + "\n")
            with self.assertRaisesRegex(WitnessRejected, "MISMATCH"):
                audit_witness(path, "0" * 64)
            with self.assertRaisesRegex(WitnessRejected, "INSUFFICIENT"):
                audit_witness(path, sha256(path.read_bytes()).hexdigest())

    def test_full_duration_synthetic_fixture_is_not_host_certification(self):
        rows = [sample(i) for i in range(1430)]
        result = verify_sequence(rows)
        self.assertGreaterEqual(result["observed_seconds"], 85200)
        self.assertFalse(result["persistent_host_24h_certified"])

    def test_external_digest_accepts_full_count_synthetic_file_only_as_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "witness.jsonl"
            rows = [sample(i) for i in range(1430)]
            path.write_text("".join(json.dumps(v) + "\n" for v in rows))
            out = audit_witness(path, sha256(path.read_bytes()).hexdigest())
            self.assertEqual(out["samples"], 1430)
            self.assertFalse(out["independent_operator_approval"])

    @patch("q21_host_witness.proc_state")
    @patch("q21_host_witness.unix_json")
    @patch("q21_host_witness.time.time_ns")
    @patch("q21_host_witness.time.monotonic_ns")
    @patch("q21_host_witness.os.statvfs")
    @patch("q21_host_witness.Path.read_text")
    def test_capture_private_operator_contract_without_market_fabrication(
        self, read_text, statvfs, monotonic_ns, wall_ns, unix, proc
    ):
        from types import SimpleNamespace

        health = {
            "operator_monitor_schema": "v36_3_private_operator_monitor_v1",
            "mode": "LIVE_SOURCE", "live_feed": True, "status": "HEALTHY",
            "operationally_healthy": True, "trading_enabled": False,
            "session_id": "f" * 32,
            "last_receiver_time_ms": 1_800_000_000_000,
            "snapshots": ["BTC-USD", "ETH-USD"],
            "subscriptions": ["level2", "market_trades", "heartbeats"],
            "connection_gaps": 0, "connection_id": 0,
            "messages": 42, "last_sequence": 41, "heartbeats": 4,
        }
        metrics = {
            "monitor_transport": "UNIX_SOCKET", "operationally_healthy": True,
            "trading_enabled": False, "execution_authorized": False,
            "max_sse_clients": 8, "active_sse_connections": 3,
        }
        unix.side_effect = [health, metrics]
        wall_ns.return_value = 1_800_000_000_500_000_000
        monotonic_ns.return_value = 222_000_000_000
        proc.return_value = (200_000_000, 80)
        statvfs.return_value = SimpleNamespace(f_bavail=100000, f_frsize=1000000)
        read_text.return_value = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            row = capture_sample(Path(tmp), 4321)
        self.assertEqual(row["messages"], 42)
        self.assertEqual(row["source_age_ms"], 500)
        self.assertFalse(row["trading_enabled"])
        self.assertNotIn("session_id", row)
        self.assertTrue(len(row["source_session_sha256"]) == 64)


if __name__ == "__main__":
    unittest.main()
