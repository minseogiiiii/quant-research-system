import hashlib
import tempfile
import unittest
from pathlib import Path

from h1_streaming_parquet import convert
from h2_native_benchmark import BASE, generate
from h7_session_walk_forward import choose, run_folds


class Forward(unittest.TestCase):
    def test_actual_epoch_replays_abstain_and_no_holdout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            entries = []
            for i in range(3):
                pin, *_ = generate(
                    root / f"raw{i}", 2000000, levels=8, start_ms=BASE + i * 60000
                )
                dest = root / f"dataset{i}"
                convert(root / f"raw{i}", pin, dest, fixture=True)
                entries.append(
                    dict(
                        dataset_path=str(dest),
                        dataset_receipt_sha256=hashlib.sha256(
                            (dest / "dataset.json").read_bytes()
                        ).hexdigest(),
                        role="DEVELOPMENT",
                    )
                )
            trials = [
                {"product": "BTC-USD", "hold_ms": 5000},
                {"product": "BTC-USD", "hold_ms": 10000},
            ]
            r = run_folds(entries, trials, 2, 5000, 42, root / "wf", fixture=True)
            self.assertEqual(r["folds"][0]["train_indices"], [0, 1])
            self.assertEqual(r["folds"][0]["test_index"], 2)
            self.assertEqual(
                r["folds"][0]["selection_status"],
                "NO_POSITIVE_ACTIVE_TRAINING_CANDIDATE",
            )
            self.assertEqual(r["diagnostic_test_count"], 0)
            self.assertFalse(r["oos_approved"])
            self.assertTrue((root / "wf" / "fold_000.db").exists())
            with self.assertRaises(ValueError):
                run_folds(
                    [*entries[:2], dict(entries[2], role="DECLARED_HOLDOUT")],
                    trials,
                    2,
                    0,
                    42,
                    root / "bad",
                    fixture=True,
                )

    def test_selection_rule_and_incomplete_inventory(self):
        a = {
            "trial_id": "a",
            "status": "COMPLETE",
            "mean_session_net_return": 0.01,
            "sessions": [{"closed_roundtrips": 1}],
        }
        b = {
            "trial_id": "b",
            "status": "COMPLETE",
            "mean_session_net_return": 0.03,
            "sessions": [{"closed_roundtrips": 1}],
        }
        self.assertEqual(choose({"all_complete": True, "results": [a, b]})[0], "b")
        self.assertIsNone(choose({"all_complete": False, "results": [a, b]})[0])
        self.assertIsNone(
            choose(
                {"all_complete": True, "results": [dict(a, mean_session_net_return=0)]}
            )[0]
        )

    def test_positive_candidate_is_actually_replayed_on_later_epoch(self):
        # Deterministic engineering fixture, not market alpha. Bid liquidity
        # depletes then recovers, followed by a large known price rise.
        from unittest.mock import patch

        from h2_native_benchmark import encode, stamp

        def wire(seq, levels, start_ms):
            t = stamp(start_ms + seq * 1000)
            events = []
            for product, base in (("BTC-USD", 50000), ("ETH-USD", 2000)):
                up = []
                if seq == 18:
                    up.extend(
                        dict(
                            side=side,
                            price_level=str(px),
                            new_quantity="0",
                            event_time=t,
                        )
                        for side, px in (("bid", base), ("offer", base + 1))
                    )
                px = base + (200 if seq >= 18 else 0)
                up.extend(
                    [
                        dict(
                            side="bid",
                            price_level=str(px),
                            new_quantity="4" if seq == 12 else "10",
                            event_time=t,
                        ),
                        dict(
                            side="offer",
                            price_level=str(px + 1),
                            new_quantity="1",
                            event_time=t,
                        ),
                    ]
                )
                events.append(
                    dict(
                        type="snapshot" if seq == 0 else "update",
                        product_id=product,
                        updates=up,
                    )
                )
            raw = encode(
                dict(channel="l2_data", timestamp=t, sequence_num=seq, events=events)
            )
            packet = dict(
                connection_id=0,
                received_utc=t,
                local_monotonic_ns=str(seq * 1000000000 + 1),
                raw_text=raw.decode(),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
            return encode(packet) + b"\n", len(raw), t

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            entries = []
            for i in range(3):
                with patch("h2_native_benchmark.wire", wire):
                    pin, *_ = generate(
                        root / f"raw{i}", 100000, levels=2, start_ms=BASE + i * 600000
                    )
                dest = root / f"dataset{i}"
                convert(root / f"raw{i}", pin, dest, fixture=True)
                entries.append(
                    dict(
                        dataset_path=str(dest),
                        dataset_receipt_sha256=hashlib.sha256(
                            (dest / "dataset.json").read_bytes()
                        ).hexdigest(),
                        role="DEVELOPMENT",
                    )
                )
            r = run_folds(
                entries,
                [{"product": "BTC-USD", "hold_ms": 5000}],
                2,
                5000,
                42,
                root / "wf",
                fixture=True,
            )
            fold = r["folds"][0]
            self.assertEqual(fold["test_status"], "DIAGNOSTIC_COMPLETE")
            self.assertGreater(fold["test_result"]["net_return"], 0)
            self.assertEqual(fold["test_result"]["closed_roundtrips"], 1)
            self.assertTrue((root / "wf" / "fold_000_selection.json").exists())
            self.assertEqual(r["diagnostic_test_count"], 1)
            self.assertFalse(r["alpha_confirmed"])
