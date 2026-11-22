"""Fail-closed final parity between a completed Coinbase archive and observer."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _q14_load_after_path_setup_0():
    from v36_observe_existing_collector import observer_source_complete

    return observer_source_complete


observer_source_complete = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


def approved():
    m = {
        "messages": 1389,
        "connections": [
            {"id": 0, "last_sequence": 1388, "messages": 1389, "heartbeat_count": 36}
        ],
    }
    h = {
        "mode": "LIVE_SOURCE",
        "live_feed": True,
        "trading_enabled": False,
        "messages": 1389,
        "last_sequence": 1388,
        "connection_id": 0,
        "connection_gaps": 0,
        "heartbeats": 36,
        "snapshots": ["BTC-USD", "ETH-USD"],
        "subscriptions": ["level2", "market_trades", "heartbeats"],
    }
    return m, h


class CollectorObserverParity(unittest.TestCase):
    def test_complete_source_accepted(self):
        m, h = approved()
        self.assertTrue(observer_source_complete(m, h, True))

    def test_reader_not_finished_rejected(self):
        m, h = approved()
        self.assertFalse(observer_source_complete(m, h, False))

    def test_observer_dropped_last_record_rejected(self):
        m, h = approved()
        h["messages"] -= 1
        h["last_sequence"] -= 1
        self.assertFalse(observer_source_complete(m, h, True))

    def test_partial_first_large_snapshot_rejected(self):
        m, h = approved()
        h["messages"] = 0
        self.assertFalse(observer_source_complete(m, h, True))

    def test_wrong_archive_counts_rejected(self):
        m, h = approved()
        m["connections"][0]["messages"] = 100
        self.assertFalse(observer_source_complete(m, h, True))

    def test_wrong_collector_sequence_rejected(self):
        m, h = approved()
        m["connections"][0]["last_sequence"] -= 1
        self.assertFalse(observer_source_complete(m, h, True))

    def test_heartbeat_mismatch_rejected(self):
        m, h = approved()
        h["heartbeats"] = 35
        self.assertFalse(observer_source_complete(m, h, True))

    def test_multiple_collector_sessions_rejected(self):
        m, h = approved()
        m["connections"].append({"id": 1, "messages": 0})
        self.assertFalse(observer_source_complete(m, h, True))

    def test_source_gap_rejected(self):
        m, h = approved()
        h["connection_gaps"] = 1
        self.assertFalse(observer_source_complete(m, h, True))

    def test_no_dual_snapshot_rejected(self):
        m, h = approved()
        h["snapshots"] = ["BTC-USD"]
        self.assertFalse(observer_source_complete(m, h, True))

    def test_incomplete_subscription_rejected(self):
        m, h = approved()
        h["subscriptions"] = ["level2"]
        self.assertFalse(observer_source_complete(m, h, True))

    def test_wrong_mode_or_execution_permission_rejected(self):
        m, h = approved()
        h["mode"] = "ARCHIVE_SIMULATION"
        self.assertFalse(observer_source_complete(m, h, True))
        m, h = approved()
        h["trading_enabled"] = True
        self.assertFalse(observer_source_complete(m, h, True))


if __name__ == "__main__":
    unittest.main()
