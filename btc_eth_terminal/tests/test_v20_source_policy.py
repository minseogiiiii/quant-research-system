"""Synthetic archive verifies legacy v13 and newer v20 admission policies."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from audit_soak import SoakAuditError, audit
    from test_v10_soak import create_archive
    from test_v13_rolling import sample_policy

    return SoakAuditError, audit, create_archive, sample_policy


SoakAuditError, audit, create_archive, sample_policy = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "source"
        mf = sample_policy(create_archive(self.root))
        mf["producer_release"] = "v20_multiday_bounded"
        mf["synthetic_fixture"] = (
            False  # Header-only test fixture, archive contents STILL synthetic.
        )
        self.mf = mf
        self.commit()

    def commit(self):
        (self.root / "manifest.json").write_text(json.dumps(self.mf))

    def test_v20_schema_retains_strict_legacy_replay(self):
        self.assertEqual(audit(self.root)["status"], "INTERNALLY_REPLAYED_NO_KNOWN_GAP")

    def test_higher_raw_cap_is_explicit_not_legacy(self):
        self.mf["rolling_policy"]["raw_budget_bytes"] = 2 * 10**9
        self.mf["rolling_policy"]["archived_jsonl_budget_bytes"] = 3 * 10**9
        self.mf["max_capture_bytes"] = 2 * 10**9
        self.mf["resource_telemetry"]["archive_budget_bytes"] = 3 * 10**9
        self.commit()
        self.assertEqual(audit(self.root)["status"], "INTERNALLY_REPLAYED_NO_KNOWN_GAP")
        self.mf["producer_release"] = "v13_rolling_bounded"
        self.commit()
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_v20_claims_of_synthetic_market_provenance_cannot_pass(self):
        self.mf["synthetic_fixture"] = True
        self.commit()
        with self.assertRaisesRegex(SoakAuditError, "Synthetic recording"):
            audit(self.root)

    def test_v20_duration_cap_is_bounded(self):
        self.mf["required_duration_seconds"] = 259201
        self.commit()
        with self.assertRaisesRegex(SoakAuditError, "duration policy"):
            audit(self.root)

    def test_v20_corrupt_resource_archive_budget_fails(self):
        self.mf["resource_telemetry"]["archive_budget_bytes"] = 111
        self.commit()
        with self.assertRaisesRegex(SoakAuditError, "resource telemetry"):
            audit(self.root)


if __name__ == "__main__":
    unittest.main()
