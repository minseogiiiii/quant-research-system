"""Synthetic-only source-window tests, never genuine exchange time attestation."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from v29_ready_window import ReadyWindowError, verify_ready_window

    return ReadyWindowError, verify_ready_window


ReadyWindowError, verify_ready_window = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

START = datetime(2026, 11, 15, tzinfo=UTC)


def stamp(t):
    return (
        (START + timedelta(seconds=t))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def fixture(seconds=86400):
    return {
        "producer_release": "v20_multiday_bounded",
        "required_duration_seconds": seconds,
        "status": "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "ready_for_research": True,
        "synthetic_fixture": False,
        "controlled_fault_injection": None,
        "faults": [],
        "total_reconnects": 0,
        "sampled_from_utc": stamp(-3),
        "finished_at_utc": stamp(seconds + 8),
        "capture_window_start_ready_utc": stamp(0),
        "capture_window_target_seconds": seconds,
        "connections": [
            {
                "became_ready": True,
                "first_ready_utc": stamp(0),
                "last_received_utc": stamp(seconds + 2),
                "snapshots": {"BTC-USD": True, "ETH-USD": True},
                "updates": {"BTC-USD": 10, "ETH-USD": 11},
                "heartbeat_count": seconds,
                "integrity_fault": False,
            }
        ],
    }


class ReadyWindowContract(TestCase):
    def test_24h_synthetic_contract(self):
        out = verify_ready_window(fixture(), mode="24h")
        self.assertEqual(out["observed_ready_window_seconds"], 86402)
        self.assertFalse(out["independent_blind_oos_approved"])

    def test_smoke_synthetic_contract(self):
        self.assertTrue(
            verify_ready_window(fixture(60), mode="smoke")[
                "source_window_contract_satisfied"
            ]
        )

    def test_wall_clock_duration_not_valid_source_duration(self):
        bad = fixture()
        bad["connections"][0]["first_ready_utc"] = stamp(30)
        bad["capture_window_start_ready_utc"] = stamp(30)
        with self.assertRaisesRegex(ReadyWindowError, "UNDERRUN"):
            verify_ready_window(bad, mode="24h")

    def test_last_raw_message_is_stale(self):
        bad = fixture()
        bad["finished_at_utc"] = stamp(86450)
        with self.assertRaisesRegex(ReadyWindowError, "STALE"):
            verify_ready_window(bad, mode="24h")

    def test_no_one_product_snapshot(self):
        bad = fixture()
        bad["connections"][0]["snapshots"]["ETH-USD"] = False
        with self.assertRaisesRegex(ReadyWindowError, "BOTH_PRODUCTS"):
            verify_ready_window(bad, mode="24h")

    def test_missing_other_product_updates(self):
        bad = fixture()
        bad["connections"][0]["updates"]["ETH-USD"] = 0
        with self.assertRaisesRegex(ReadyWindowError, "BOTH_PRODUCTS"):
            verify_ready_window(bad, mode="24h")

    def test_no_heartbeat(self):
        bad = fixture()
        bad["connections"][0]["heartbeat_count"] = 0
        with self.assertRaisesRegex(ReadyWindowError, "BOTH_PRODUCTS"):
            verify_ready_window(bad, mode="24h")

    def test_multiple_connections(self):
        bad = fixture()
        bad["connections"] *= 2
        with self.assertRaisesRegex(ReadyWindowError, "NOT_ONE_CONNECTION"):
            verify_ready_window(bad, mode="24h")

    def test_reconnect_counter_nonzero(self):
        bad = fixture()
        bad["total_reconnects"] = 1
        with self.assertRaisesRegex(ReadyWindowError, "NOT_CONTINUOUS"):
            verify_ready_window(bad, mode="24h")

    def test_fault_recorded(self):
        bad = fixture()
        bad["faults"] = [{"reason": "disconnect"}]
        with self.assertRaisesRegex(ReadyWindowError, "NOT_CONTINUOUS"):
            verify_ready_window(bad, mode="24h")

    def test_synthetic_fixture_rejected(self):
        bad = fixture()
        bad["synthetic_fixture"] = True
        with self.assertRaisesRegex(ReadyWindowError, "NOT_CONTINUOUS"):
            verify_ready_window(bad, mode="24h")

    def test_legacy_without_readiness_metadata(self):
        bad = fixture()
        bad.pop("capture_window_start_ready_utc")
        with self.assertRaisesRegex(ReadyWindowError, "READINESS_BASIS"):
            verify_ready_window(bad, mode="24h")

    def test_legacy_producer(self):
        bad = fixture()
        bad["producer_release"] = "v14"
        with self.assertRaisesRegex(ReadyWindowError, "LEGACY"):
            verify_ready_window(bad, mode="24h")

    def test_wrong_duration(self):
        with self.assertRaisesRegex(ReadyWindowError, "REQUESTED_DURATION"):
            verify_ready_window(fixture(60), mode="24h")

    def test_invalid_timestamp(self):
        bad = fixture()
        bad["connections"][0]["last_received_utc"] = "bogus"
        with self.assertRaisesRegex(ReadyWindowError, "INVALID_WINDOW_TIME"):
            verify_ready_window(bad, mode="24h")

    def test_node_collector_waits_for_two_snapshots_before_duration(self):
        import os
        import shutil
        import subprocess
        from tempfile import TemporaryDirectory

        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        with TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(
                {
                    "SOAK_TEST_FIXTURE": "1",
                    "SOAK_DURATION_SECONDS": "15",
                    "SOAK_DISK_RESERVE_BYTES": "0",
                    "SOAK_CHUNK_LIMIT_BYTES": "1024",
                    "SOAK_RAW_BUDGET_BYTES": "1000000",
                    "SOAK_ARCHIVE_BUDGET_BYTES": "1200000",
                }
            )
            cp = subprocess.run(
                [
                    "node",
                    "--import",
                    str(ROOT / "tests/mock_coinbase_periodic.mjs"),
                    str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
                ],
                cwd=temporary,
                env=env,
                capture_output=True,
                text=True,
                timeout=29,
            )
            sources = list(
                (Path(temporary) / "soak_evidence").glob("coinbase_btc_eth_soak_*")
            )
            self.assertEqual(len(sources), 1, cp.stdout + cp.stderr)
            manifest = json.loads((sources[0] / "manifest.json").read_bytes())
            self.assertEqual(manifest["finish_reason"], "DURATION_REACHED")
            self.assertEqual(manifest["capture_window_target_seconds"], 15)
            con = manifest["connections"][0]
            self.assertTrue(con["snapshots"]["BTC-USD"] and con["snapshots"]["ETH-USD"])
            elapsed = (
                datetime.fromisoformat(con["last_received_utc"].replace("Z", "+00:00"))
                - datetime.fromisoformat(con["first_ready_utc"].replace("Z", "+00:00"))
            ).total_seconds()
            self.assertGreaterEqual(elapsed, 15)
            self.assertFalse(manifest["ready_for_research"])
            self.assertNotEqual(cp.returncode, 0)

    def test_existing_real_v27_source_never_false_upgrade(self):
        path = ROOT / "evidence/v27_github_real_60s"
        manifests = list(path.glob("source/coinbase_btc_eth_soak_*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        actual = json.loads(manifests[0].read_bytes())
        with self.assertRaises(ReadyWindowError):
            verify_ready_window(actual, mode="smoke")
