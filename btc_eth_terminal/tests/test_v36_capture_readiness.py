"""No-data/public WebSocket failures must never pass capture release gates."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
def _q14_load_after_path_setup_0():
    from v36_observe_existing_collector import capture_ready_for_research
    return capture_ready_for_research

capture_ready_for_research = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


def clean_manifest():
    return {
        "format": "btc_eth_raw_soak_v10",
        "status": "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "ready_for_research": True,
        "messages": 7,
        "faults": [],
        "complete_sequence_per_connection": True,
        "all_connections_have_snapshots": True,
    }


class CaptureExitIntegrity(unittest.TestCase):
    def test_clean_capture_only(self):
        self.assertTrue(capture_ready_for_research(0, clean_manifest()))

    def test_no_messages_never_succeeds_even_if_node_exits_zero(self):
        m = clean_manifest()
        m["messages"] = 0
        self.assertFalse(capture_ready_for_research(0, m))

    def test_nonzero_collector_exit_rejected(self):
        self.assertFalse(capture_ready_for_research(2, clean_manifest()))

    def test_incomplete_capture_status_rejected(self):
        m = clean_manifest()
        m["status"] = "CAPTURE_INCOMPLETE_OR_INVALID"
        self.assertFalse(capture_ready_for_research(0, m))

    def test_fault_and_reconnect_quarantine_rejected(self):
        m = clean_manifest()
        m["faults"] = [{"reason": "NETWORK"}]
        self.assertFalse(capture_ready_for_research(0, m))

    def test_missing_source_snapshot_rejected(self):
        m = clean_manifest()
        m["all_connections_have_snapshots"] = False
        self.assertFalse(capture_ready_for_research(0, m))

    def test_missing_sequence_rejected(self):
        m = clean_manifest()
        m["complete_sequence_per_connection"] = False
        self.assertFalse(capture_ready_for_research(0, m))

    def test_malformed_or_forged_manifest_rejected(self):
        self.assertFalse(capture_ready_for_research(0, None))
        m = clean_manifest()
        m["messages"] = True
        self.assertFalse(capture_ready_for_research(0, m))
        m = clean_manifest()
        m["format"] = "not-a-capture"
        self.assertFalse(capture_ready_for_research(0, m))


if __name__ == "__main__":
    unittest.main()
