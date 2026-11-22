"""v18 tests prove label separation, source integrity, and bounded receipt-time sampling."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))


def _q14_load_after_path_setup_0():
    from post_quote_markout import IndexedBook, MarkoutError, MarkoutPolicy, inspect
    from taker_quote_envelope import Assumptions, Book, EnvelopeError
    from test_v10_soak import create_archive

    return (
        IndexedBook,
        MarkoutError,
        MarkoutPolicy,
        inspect,
        Assumptions,
        Book,
        EnvelopeError,
        create_archive,
    )


(
    IndexedBook,
    MarkoutError,
    MarkoutPolicy,
    inspect,
    Assumptions,
    Book,
    EnvelopeError,
    create_archive,
) = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class V18MarkoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.src = Path(self.temp.name) / "toy"

        def mutate(messages):
            # Existing quote sequence 3 shows BTC mid 100.5.
            # Future sequence 6 moves BTC ask from 101 to 102, mid to 101.
            t = messages[6]["timestamp"]
            messages[6]["channel"] = "l2_data"
            messages[6]["events"] = [
                {
                    "type": "update",
                    "product_id": "BTC-USD",
                    "updates": [
                        {
                            "side": "offer",
                            "price_level": "101",
                            "new_quantity": "0",
                            "event_time": t,
                        },
                        {
                            "side": "offer",
                            "price_level": "102",
                            "new_quantity": "2",
                            "event_time": t,
                        },
                    ],
                }
            ]

        create_archive(self.src, mutate=mutate)
        manifest = json.loads((self.src / "manifest.json").read_text())
        manifest["connections"][0]["updates"]["BTC-USD"] = 2
        for chunk in manifest["chunks"]:
            file = self.src / chunk["file"]
            rebuilt = []
            for raw in file.read_text().splitlines():
                row = json.loads(raw)
                seq = json.loads(row["raw_text"])["sequence_num"]
                row["local_monotonic_ns"] = str((seq + 1) * 1_000_000_000)
                rebuilt.append(json.dumps(row, separators=(",", ":")) + "\n")
            blob = "".join(rebuilt).encode()
            file.write_bytes(blob)
            chunk["archive_sha256"] = sha256(blob).hexdigest()
            chunk["archived_line_bytes"] = len(blob)
            chunk["raw_text_bytes"] = sum(
                len(json.loads(x)["raw_text"].encode()) for x in rebuilt
            )
        manifest["bytes"] = sum(x["raw_text_bytes"] for x in manifest["chunks"])
        (self.src / "manifest.json").write_text(json.dumps(manifest))
        self.assumptions = Assumptions(1000, 4000, 3000, "10", "5", "0.5")
        self.intents = [
            {
                "id": "toy_buy",
                "product_id": "BTC-USD",
                "side": "BUY",
                "quantity": "0.5",
                "signal_sequence": 2,
            }
        ]

    def check(self, horizons=(1000, 2000, 4000), tolerance=2500):
        return inspect(
            self.src, self.intents, self.assumptions, MarkoutPolicy(horizons, tolerance)
        )

    def test_future_markout_horizon_is_first_later_product_packet(self):
        result = self.check()
        item = result["results"][0]
        self.assertEqual(item["quote_sequence"], 3)
        self.assertEqual(item["quote_midprice"], "100.5")
        self.assertEqual(
            [x["status"] for x in item["labels"]],
            ["HISTORICAL_FUTURE_MIDPRICE_LABEL_ONLY"] * 2
            + ["LABEL_NOT_AVAILABLE_BEFORE_SOURCE_END"],
        )
        for future in item["labels"][:2]:
            self.assertEqual(future["label_observed_at_sequence"], 6)
            self.assertEqual(future["future_midprice"], "101")
            self.assertEqual(future["observed_delay_ms"], "3000")
            self.assertGreater(float(future["side_signed_midprice_move_bps"]), 0)
            self.assertTrue(future["label_not_available_to_signal"])
        self.assertFalse(result["actual_order_fills_verified"])
        self.assertFalse(result["strategy_alpha_or_profitability_verified"])
        self.assertFalse(result["maker_queue_rank_known"])

    def test_opposite_side_markout_sign_is_negative(self):
        self.intents[0]["side"] = "SELL"
        item = self.check((1000,))["results"][0]
        self.assertLess(float(item["labels"][0]["side_signed_midprice_move_bps"]), 0)

    def test_tolerance_rejects_late_first_observation(self):
        item = self.check((1000,), 100)["results"][0]
        self.assertEqual(
            item["labels"][0]["status"], "LABEL_NOT_AVAILABLE_WITHIN_TOLERANCE"
        )
        self.assertNotIn("future_midprice", item["labels"][0])

    def test_signal_before_both_snapshots_cannot_get_label(self):
        self.intents[0]["signal_sequence"] = 1
        result = self.check((1000,))["results"][0]
        self.assertEqual(result["status"], "NO_FUTURE_LABEL_WITHOUT_VALID_QUOTE")
        self.assertEqual(result["labels"], [])

    def test_strict_horizon_constraints(self):
        for h in (
            (),
            (0,),
            (99,),
            (-100,),
            ("1000",),
            (1000, 1000),
            (5000, 1000),
            (60001,),
            (True,),
            tuple(range(7)),
        ):
            with (
                self.subTest(horizons=h),
                self.assertRaises((EnvelopeError, MarkoutError)),
            ):
                self.check(h)
        for t in (0, -1, 10001, "1000", True):
            with self.subTest(tolerance=t), self.assertRaises(MarkoutError):
                self.check((1000,), t)

    def test_source_mutation_after_quote_admission_is_rejected(self):
        from post_quote_markout import evaluate as underlying

        def tamper(*args):
            result = underlying(*args)
            f = self.src / "chunks/chunk_00001_conn_000.jsonl"
            f.write_bytes(f.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
            return result

        with patch("post_quote_markout.evaluate", side_effect=tamper):
            with self.assertRaisesRegex(MarkoutError, "mutated after first quote"):
                self.check()

    def test_quarantined_archive_blocks_all_labels(self):
        manifest = json.loads((self.src / "manifest.json").read_text())
        manifest["status"] = "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
        manifest["ready_for_research"] = False
        manifest["faults"] = [{"connection_id": 0, "reason": "INJECTED_DISCONNECT"}]
        (self.src / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(EnvelopeError):
            self.check()

    def test_duplicate_intent_id_rejected_by_existing_gate(self):
        self.intents.append(dict(self.intents[0]))
        with self.assertRaises(EnvelopeError):
            self.check()

    def test_no_return_label_when_future_packet_missing(self):
        # A future heartbeat/trade does not count as a product L2 update.
        # The toy positive scenario's only future BTC update is sequence 6.
        item = self.check((4000,), 2500)["results"][0]
        self.assertEqual(
            item["labels"][0]["status"], "LABEL_NOT_AVAILABLE_BEFORE_SOURCE_END"
        )

    def test_real_source_first_eligible_label_never_overwritten(self):
        source = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
        r = inspect(
            source,
            [
                {
                    "id": "frozen_real",
                    "product_id": "BTC-USD",
                    "side": "BUY",
                    "quantity": "0.001",
                    "signal_sequence": 100,
                }
            ],
            Assumptions(250, 5000, 2500, "10", "5", "0.25"),
            MarkoutPolicy((1000, 5000, 30000), 2500),
        )
        entry = r["results"][0]
        self.assertEqual(entry["quote_sequence"], 116)
        self.assertEqual(
            [x["label_observed_at_sequence"] for x in entry["labels"]], [160, 321, 1414]
        )
        self.assertEqual(
            [x["horizon_ms"] for x in entry["labels"]], [1000, 5000, 30000]
        )
        self.assertTrue(
            all(x["label_not_available_to_signal"] for x in entry["labels"])
        )
        self.assertFalse(r["actual_order_fills_verified"])

    def test_future_label_mutation_never_changes_past_selected_quote(self):
        first = self.check((1000,))["results"][0]
        mf = json.loads((self.src / "manifest.json").read_text())
        c = mf["chunks"][1]
        path = self.src / c["file"]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            msg = json.loads(row["raw_text"])
            if msg["sequence_num"] == 6:
                msg["events"][0]["updates"][1]["price_level"] = "103"
                row["raw_text"] = json.dumps(msg, separators=(",", ":"))
                row["sha256"] = sha256(row["raw_text"].encode()).hexdigest()
        blob = "".join(
            json.dumps(row, separators=(",", ":")) + "\n" for row in rows
        ).encode()
        path.write_bytes(blob)
        c["archive_sha256"] = sha256(blob).hexdigest()
        c["archived_line_bytes"] = len(blob)
        c["raw_text_bytes"] = sum(len(x["raw_text"].encode()) for x in rows)
        mf["bytes"] = sum(x["raw_text_bytes"] for x in mf["chunks"])
        (self.src / "manifest.json").write_text(json.dumps(mf))
        second = self.check((1000,))["results"][0]
        self.assertEqual(first["quote_sequence"], second["quote_sequence"])
        self.assertEqual(first["quote_midprice"], second["quote_midprice"])
        self.assertEqual(
            first["labels"][0]["label_observed_at_sequence"],
            second["labels"][0]["label_observed_at_sequence"],
        )
        self.assertNotEqual(
            first["labels"][0]["side_signed_midprice_move_bps"],
            second["labels"][0]["side_signed_midprice_move_bps"],
        )

    def test_unique_price_heap_matches_reference_after_repeated_updates(self):
        fast, reference = IndexedBook(), Book()
        from decimal import Decimal

        for i in range(2500):
            side = "bid" if i % 2 == 0 else "offer"
            price = (
                Decimal(100 - (i % 12)) if side == "bid" else Decimal(105 + (i % 12))
            )
            q = Decimal(0) if i % 13 == 0 else Decimal(1 + (i % 7))
            fast.change(side, price, q)
            reference.change(side, price, q)
            if i % 19 == 0:
                self.assertEqual(fast.best("bid"), reference.best("bid"))
                self.assertEqual(fast.best("offer"), reference.best("offer"))
        # Removing top and restoring the exact same price must not create a stale duplicate.
        px = Decimal(100)
        fast.change("bid", px, Decimal(0))
        reference.change("bid", px, Decimal(0))
        fast.change("bid", px, Decimal("3"))
        reference.change("bid", px, Decimal("3"))
        self.assertEqual(fast.best("bid"), reference.best("bid"))
        self.assertEqual(fast.best("offer"), reference.best("offer"))
        self.assertLessEqual(len(fast.bid_heap), 4 * len(fast.bid) + 1024)

    def test_archive_clocks_are_not_broker_latency(self):
        result = self.check((1000,))
        self.assertIn("LOCAL_MONOTONIC", result["receipt_clock"])
        self.assertFalse(result["broker_latency_calibrated"])
        self.assertTrue(result["future_labels_are_never_input_features"])
        self.assertFalse(result["orders_enabled"])


if __name__ == "__main__":
    unittest.main()
