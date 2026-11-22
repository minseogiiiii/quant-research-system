"""Synthetic, fail-closed static-depth *observations*, not exchange fills."""

import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
from static_depth_stress import DepthPolicy, DepthStressError, diagnose
from taker_quote_envelope import Assumptions, EnvelopeError
from test_v10_soak import create_archive


class DepthStressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "source"

        def extend(events):
            # Immutable test book with visible offers at 101 (2 base)
            # and 102 (6 base), bids at 100 (3) and 99 (4).
            events[1]["events"][0]["updates"].extend(
                [
                    {
                        "side": "offer",
                        "price_level": "102",
                        "new_quantity": "6",
                        "event_time": events[1]["timestamp"],
                    },
                    {
                        "side": "bid",
                        "price_level": "99",
                        "new_quantity": "4",
                        "event_time": events[1]["timestamp"],
                    },
                ]
            )

        create_archive(self.src, mutate=extend)
        mf = json.loads((self.src / "manifest.json").read_text())
        for c in mf["chunks"]:
            p = self.src / c["file"]
            new = []
            for line in p.read_text().splitlines():
                obj = json.loads(line)
                seq = json.loads(obj["raw_text"])["sequence_num"]
                obj["local_monotonic_ns"] = str((seq + 1) * 1_000_000_000)
                new.append(json.dumps(obj, separators=(",", ":")) + "\n")
            raw = "".join(new).encode()
            p.write_bytes(raw)
            c["archive_sha256"] = hashlib.sha256(raw).hexdigest()
            c["archived_line_bytes"] = len(raw)
        (self.src / "manifest.json").write_text(json.dumps(mf))
        self.assumptions = Assumptions(1000, 4000, 3000, "10", "5", "0.5")

    def obs(self, side="BUY", qty="1.5", sequence=2, product="BTC-USD"):
        return [
            {
                "id": "fixed",
                "product_id": product,
                "side": side,
                "quantity": qty,
                "signal_sequence": sequence,
            }
        ]

    def check(self, max_levels=3, max_sweep_bps="100", **kwargs):
        return diagnose(
            self.src,
            self.obs(**kwargs),
            self.assumptions,
            DepthPolicy(max_levels, max_sweep_bps),
        )["results"][0]

    def test_first_post_delay_quote_is_same_as_v15(self):
        r = self.check()
        self.assertEqual(r["quote_sequence"], 3)
        self.assertEqual(r["status"], "STATIC_L2_DEPTH_REFERENCE_NOT_FILL")
        self.assertEqual(
            r["original_quote_status"], "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH"
        )
        self.assertEqual([v["price"] for v in r["levels_referenced"]], ["101", "102"])
        self.assertEqual(r["available_haircut_quantity_within_levels"], "4.0")
        self.assertEqual(Decimal(r["static_vwap"]), Decimal("152") / Decimal("1.5"))

    def test_one_level_rejects_when_best_level_haircut_insufficient(self):
        r = self.check(max_levels=1)
        self.assertEqual(
            r["status"], "REJECTED_STATIC_DEPTH_NOT_DEMONSTRATED_WITHIN_LEVEL_LIMIT"
        )
        self.assertEqual(r["quantity_uncovered"], "0.5")
        self.assertNotIn("hypothetical_cashflow_usd", r)

    def test_sweep_cap_zero_denies_two_level_sweep(self):
        r = self.check(max_levels=3, max_sweep_bps="0")
        self.assertEqual(r["status"], "REJECTED_STATIC_SWEEP_EXCEEDS_POLICY")
        self.assertNotIn("static_vwap", r)
        self.assertNotIn("hypothetical_cashflow_usd", r)

    def test_one_level_reference_matches_v15_quote_cashflow(self):
        from taker_quote_envelope import evaluate

        q = evaluate(self.src, self.obs(qty="0.5"), self.assumptions)["results"][0]
        d = self.check(qty="0.5", max_levels=1)
        self.assertEqual(q["hypothetical_cashflow_usd"], d["hypothetical_cashflow_usd"])
        self.assertEqual(
            q["hypothetical_quoted_price"], d["static_vwap_with_hypothetical_slippage"]
        )

    def test_sell_walks_best_bids_only(self):
        d = self.check(side="SELL", qty="3", max_levels=3)
        self.assertEqual(d["status"], "STATIC_L2_DEPTH_REFERENCE_NOT_FILL")
        self.assertEqual([v["price"] for v in d["levels_referenced"]], ["100", "99"])
        self.assertGreater(float(d["hypothetical_cashflow_usd"]), 0)
        self.assertEqual(Decimal(d["static_vwap"]), Decimal("299") / Decimal("3"))

    def test_thinner_capacity_rejects_larger_order(self):
        p = Assumptions(1000, 4000, 3000, "10", "5", "0.1")
        d = diagnose(self.src, self.obs(qty="1.5"), p, DepthPolicy(3, "100"))[
            "results"
        ][0]
        self.assertEqual(
            d["status"], "REJECTED_STATIC_DEPTH_NOT_DEMONSTRATED_WITHIN_LEVEL_LIMIT"
        )
        self.assertNotIn("hypothetical_cashflow_usd", d)

    def test_eth_still_observes_its_own_first_l2_packet(self):
        r = self.check(product="ETH-USD", qty="0.2")
        self.assertEqual(r["quote_sequence"], 4)

    def test_early_signal_still_rejected_not_repriced(self):
        r = self.check(sequence=1)
        self.assertEqual(r["status"], "REJECTED_NOT_READY_AT_SIGNAL")
        self.assertIsNone(r["quote_sequence"])

    def test_predeclared_policy_validation(self):
        for levels in (-1, 0, 51, True, "10"):
            with self.subTest(levels=levels), self.assertRaises(EnvelopeError):
                self.check(max_levels=levels)
        for max_sweep in ("-1", "NaN", "Infinity", "100000", "notdecimal"):
            with self.subTest(limit=max_sweep), self.assertRaises(EnvelopeError):
                self.check(max_sweep_bps=max_sweep)

    def test_quarantined_source_is_not_usable(self):
        mf = json.loads((self.src / "manifest.json").read_text())
        mf["status"] = "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
        mf["ready_for_research"] = False
        mf["faults"] = [{"connection_id": 0, "reason": "DISCONNECT"}]
        (self.src / "manifest.json").write_text(json.dumps(mf))
        with self.assertRaises(EnvelopeError):
            self.check()

    def test_mutation_after_original_admission_fails(self):
        from static_depth_stress import evaluate as evaluate_real

        def tamper(*args):
            result = evaluate_real(*args)
            p = self.src / "chunks/chunk_00001_conn_000.jsonl"
            p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
            return result

        with patch("static_depth_stress.evaluate", side_effect=tamper):
            with self.assertRaisesRegex(DepthStressError, "Changed raw source"):
                self.check()

    def test_real_clean_source_uses_first_post_delay_quote(self):
        source = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
        r = diagnose(
            source,
            [
                {
                    "id": "real100",
                    "product_id": "BTC-USD",
                    "side": "BUY",
                    "quantity": "0.001",
                    "signal_sequence": 100,
                }
            ],
            Assumptions(250, 5000, 2500, "10", "5", "0.25"),
            DepthPolicy(5, "10"),
        )
        self.assertEqual(r["source_messages_audited"], 5177)
        self.assertEqual(r["results"][0]["quote_sequence"], 116)
        self.assertEqual(
            r["results"][0]["status"], "STATIC_L2_DEPTH_REFERENCE_NOT_FILL"
        )
        self.assertFalse(r["actual_fills_verified"])

    def test_real_quarantined_source_is_rejected(self):
        source = (
            ROOT / "backend/data/live_v14_disconnect_120s/coinbase_btc_eth_soak_gEF6uZ"
        )
        with self.assertRaises(EnvelopeError):
            diagnose(
                source,
                [
                    {
                        "id": "q",
                        "product_id": "BTC-USD",
                        "side": "BUY",
                        "quantity": "0.001",
                        "signal_sequence": 100,
                    }
                ],
                Assumptions(250, 5000, 2500, "10", "5", "0.25"),
                DepthPolicy(5, "10"),
            )

    def test_no_profit_or_orders_claim(self):
        report = diagnose(self.src, self.obs(), self.assumptions, DepthPolicy(3, "100"))
        self.assertFalse(report["actual_order_sent"])
        self.assertFalse(report["actual_fills_verified"])
        self.assertFalse(report["market_impact_verified"])
        self.assertFalse(report["queue_priority_verified"])
        self.assertTrue(report["no_strategy_optimization_or_pnl"])


if __name__ == "__main__":
    unittest.main()
