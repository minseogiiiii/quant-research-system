"""v21 adversarial/causality tests, no synthetic outcomes promoted as market data."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))


def _q14_load_after_path_setup_0():
    from original_pair_audit_v20 import ProvenanceError, audit
    from test_v20_pair_audit import SourceAuditTests, fhash
    from v21_evidence_inventory import inventory
    from v21_source_evidence import asof_rows

    return ProvenanceError, audit, SourceAuditTests, fhash, inventory, asof_rows


ProvenanceError, audit, SourceAuditTests, fhash, inventory, asof_rows = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0


class StrictPairs(SourceAuditTests):
    def test_mismatched_product_filename_cohorts(self):
        new = "eth_future_labels.csv.gz"
        (self.root / self.lname).rename(self.root / new)
        self.manifest["future_labels"]["path"] = new
        with self.assertRaisesRegex(ProvenanceError, "prefixes disagree"):
            audit(self.root, self.manifest)

    def test_five_minute_gap_rejected(self):
        self.features.pop(1)
        self.labels.pop(1)
        self.save()
        self.manifest["feature"]["sha256"] = fhash(self.root / self.fname)
        self.manifest["future_labels"]["sha256"] = fhash(self.root / self.lname)
        with self.assertRaisesRegex(ProvenanceError, "Discontinuous"):
            audit(self.root, self.manifest)

    def test_coinbase_perpetual_claim_rejected(self):
        self.manifest["market_type"] = "perpetual"
        with self.assertRaisesRegex(ProvenanceError, "spot, not perpetual"):
            audit(self.root, self.manifest)

    def test_self_attested_raw_archive_rejected(self):
        self.manifest["feature"]["raw_source_path"] = self.fname
        self.manifest["feature"]["raw_source_sha256"] = fhash(self.root / self.fname)
        with self.assertRaisesRegex(ProvenanceError, "self-attest"):
            audit(self.root, self.manifest)


class InventoryTests(unittest.TestCase):
    def test_missing_candidates_not_called_verified(self):
        with tempfile.TemporaryDirectory() as d:
            x = inventory([Path(d)])
            self.assertIn("BLOCKED_", x["status"])
            self.assertFalse(x["eligible_for_blind_oos"])

    def test_separate_direct_and_zip_candidates_not_original_certification(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            direct = d / "x_features_5m.csv.gz"
            direct.write_bytes(b"mock_only")
            with ZipFile(d / "old.zip", "w") as z:
                z.writestr("archives/y_future_labels.csv.gz", b"not_a_provenance")
            r = inventory([d])
            self.assertEqual(len(r["direct_candidates"]), 1)
            self.assertEqual(
                r["direct_candidates"][0]["sha256"], sha256(b"mock_only").hexdigest()
            )
            self.assertEqual(len(r["zip_member_candidates"]), 1)
            self.assertFalse(r["actual_original_pair_source_provenance_verified"])
            self.assertFalse(r["point_in_time_availability_verified"])


class PITCausality(unittest.TestCase):
    t = 1794096000000

    def stream(self):
        t = self.t
        return [
            (
                t,
                0,
                "BTC-USD",
                "snapshot",
                [
                    {"side": "bid", "price_level": "100", "new_quantity": "2"},
                    {"side": "offer", "price_level": "101", "new_quantity": "3"},
                ],
            ),
            (
                t + 1,
                1,
                "ETH-USD",
                "snapshot",
                [
                    {"side": "bid", "price_level": "200", "new_quantity": "2"},
                    {"side": "offer", "price_level": "202", "new_quantity": "1"},
                ],
            ),
            (
                t + 2000,
                2,
                "BTC-USD",
                "update",
                [{"side": "bid", "price_level": "100", "new_quantity": "4"}],
            ),
        ]

    def test_future_mutation_does_not_change_past_quote(self):
        a = self.stream()
        b = [
            *a,
            (
                self.t + 3000,
                3,
                "BTC-USD",
                "update",
                [
                    {"side": "offer", "price_level": "101", "new_quantity": "0"},
                    {"side": "offer", "price_level": "110", "new_quantity": "4"},
                ],
            ),
        ]
        initial = asof_rows(a, [self.t + 500, self.t + 3500])
        modified = asof_rows(b, [self.t + 500, self.t + 3500])
        self.assertEqual(initial[:2], modified[:2])
        self.assertNotEqual(initial[2], modified[2])
        self.assertEqual(initial[0]["best_bid"], "100")
        self.assertEqual(initial[1]["status"], "RECEIVER_ASOF_L2_QUOTE")

    def test_stale_quote_blocked(self):
        x = asof_rows(self.stream(), [self.t + 11000])
        self.assertEqual(x[0]["status"], "NO_ADMISSIBLE_FRESH_QUOTE")

    def test_update_preceding_snapshot_blocked(self):
        e = [
            (
                self.t,
                0,
                "BTC-USD",
                "update",
                [{"side": "bid", "price_level": "1", "new_quantity": "2"}],
            )
        ]
        with self.assertRaisesRegex(ValueError, "Update preceding"):
            asof_rows(e, [self.t])

    def test_reverse_source_time_blocked(self):
        a = self.stream()
        with self.assertRaisesRegex(ValueError, "ordering"):
            asof_rows([a[1], a[0]], [self.t + 1])

    def test_crossed_book_blocked(self):
        a = self.stream() + [
            (
                self.t + 2500,
                3,
                "BTC-USD",
                "update",
                [{"side": "offer", "price_level": "90", "new_quantity": "3"}],
            )
        ]
        with self.assertRaisesRegex(ValueError, "Crossed"):
            asof_rows(a, [self.t + 4000])


if __name__ == "__main__":
    unittest.main()


class IndependentParityNegativeTests(unittest.TestCase):
    def test_changed_quote_cannot_pass_second_software_audit(self):
        from unittest.mock import patch

        import independent_v21_asof_audit as check
        from v21_source_evidence import asof_rows

        m = "f" * 64
        base = asof_rows(PITCausality().stream(), [PITCausality.t + 500])
        # Build a minimal representative primary report and corrupt the second path.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            report = root / "primary.json"
            report.write_text(
                json.dumps(
                    {
                        "source_manifest_sha256": m,
                        "source_message_count_independently_admitted": 3,
                        "source_books_final_sha256": {"BTC-USD": "a", "ETH-USD": "b"},
                        "rows": base,
                    }
                )
            )
            other = []
            for x in base:
                row = {
                    k: v
                    for k, v in x.items()
                    if k
                    not in (
                        "clock_calibration_verified",
                        "independent_venue_reference_verified",
                        "original_user_features_verified",
                        "possible_real_order_fill_verified",
                    )
                }
                other.append(row)
            other[0]["best_bid"] = "101"
            fake = {
                "source_sha256": m,
                "source_replay_messages": 3,
                "source_books_final_sha256": {"BTC-USD": "a", "ETH-USD": "b"},
                "rows": other,
            }
            with patch.object(check, "independent_quotes", return_value=fake):
                with self.assertRaisesRegex(ValueError, "discrepancy"):
                    check.audit(root, report)
