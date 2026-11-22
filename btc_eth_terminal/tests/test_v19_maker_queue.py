"""v19 negative controls: no public trade is an authenticated user fill."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))


def _q14_load_after_path_setup_0():
    from audit_soak import SoakAuditError
    from maker_queue_uncertainty import MakerPolicy, MakerQueueError, inspect
    from taker_quote_envelope import Assumptions, EnvelopeError
    from test_v10_soak import create_archive

    return (
        SoakAuditError,
        MakerPolicy,
        MakerQueueError,
        inspect,
        Assumptions,
        EnvelopeError,
        create_archive,
    )


(
    SoakAuditError,
    MakerPolicy,
    MakerQueueError,
    inspect,
    Assumptions,
    EnvelopeError,
    create_archive,
) = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class V19MakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"

        def change(messages):
            trade = messages[6]["events"][0]["trades"][0]
            trade["price"] = "100"
            trade["size"] = "4.2"
            trade["side"] = "SELL"  # semantic not used for primary flow

        create_archive(self.src, mutate=change)
        self._seal()
        self.intents = [
            {
                "id": "fixture_maker",
                "product_id": "BTC-USD",
                "side": "BUY",
                "quantity": "0.5",
                "signal_sequence": 2,
            }
        ]
        self.assumptions = Assumptions(1000, 5000, 3000, "10", "5", "0.25")

    def _seal(self):
        mf = json.loads((self.src / "manifest.json").read_text())
        for chunk in mf["chunks"]:
            path = self.src / chunk["file"]
            rows = []
            for l in path.read_text().splitlines():
                row = json.loads(l)
                seq = json.loads(row["raw_text"])["sequence_num"]
                row["local_monotonic_ns"] = str((seq + 1) * 1_000_000_000)
                row["sha256"] = sha256(row["raw_text"].encode()).hexdigest()
                rows.append(json.dumps(row, separators=(",", ":")) + "\n")
            body = "".join(rows).encode()
            path.write_bytes(body)
            chunk["archive_sha256"] = sha256(body).hexdigest()
            chunk["archived_line_bytes"] = len(body)
            chunk["raw_text_bytes"] = sum(
                len(json.loads(l)["raw_text"].encode()) for l in rows
            )
        mf["bytes"] = sum(c["raw_text_bytes"] for c in mf["chunks"])
        (self.src / "manifest.json").write_text(json.dumps(mf))

    def check(self, horizon=3000, intents=None):
        return inspect(
            self.src,
            intents or self.intents,
            self.assumptions,
            MakerPolicy(horizon, "10"),
        )

    def test_conditionals_not_fills_and_preserve_first_quote(self):
        data = self.check()
        c = data["results"][0]
        self.assertEqual(data["source_messages_audited"], 7)
        self.assertEqual(c["anchor_sequence"], 3)
        self.assertEqual(c["posted_price_hypothetical"], "100")
        self.assertEqual(c["initial_visible_same_side_quantity"], "4")
        self.assertEqual(c["eligible_trade_volume_any_side_exact_price"], "4.2")
        self.assertEqual(c["hypothetical_no_ahead_queue_quantity"], "0.5")
        self.assertEqual(c["hypothetical_fifo_no_ahead_cancellations_quantity"], "0.2")
        self.assertEqual(
            c["hypothetical_fifo_no_ahead_cancellations_fill_fraction"], "0.4"
        )
        self.assertFalse(data["actual_fills_verified"])
        self.assertFalse(data["actual_fill_probability_estimated"])
        self.assertFalse(data["real_orders_submitted"])
        self.assertIsNone(c["verified_actual_fill_quantity"])
        self.assertEqual(c["observed_user_fill_lower_bound"], "0")

    def test_incomplete_horizon_has_no_scenario_quantity(self):
        c = self.check(5000)["results"][0]
        self.assertEqual(c["status"], "INCOMPLETE_RESTING_WINDOW")
        self.assertIsNone(c["hypothetical_no_ahead_queue_quantity"])
        self.assertIsNone(c["hypothetical_fifo_no_ahead_cancellations_quantity"])

    def test_nonmatching_price_has_zero_flow(self):
        mf = json.loads((self.src / "manifest.json").read_text())
        p = self.src / mf["chunks"][1]["file"]
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        for row in rows:
            msg = json.loads(row["raw_text"])
            if msg["sequence_num"] == 6:
                msg["events"][0]["trades"][0]["price"] = "100.5"
                row["raw_text"] = json.dumps(msg, separators=(",", ":"))
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self._seal()
        c = self.check()["results"][0]
        self.assertEqual(c["eligible_trade_volume_any_side_exact_price"], "0")
        self.assertEqual(c["hypothetical_no_ahead_queue_quantity"], "0")

    def test_no_eligible_quote_no_queue_inference(self):
        bad = [dict(self.intents[0], signal_sequence=0)]
        c = self.check(intents=bad)["results"][0]
        self.assertEqual(c["status"], "NO_ADMITTED_MAKER_ANCHOR")
        self.assertNotIn("hypothetical_no_ahead_queue_quantity", c)

    def test_corrupt_archive_rejected(self):
        p = self.src / "chunks/chunk_00001_conn_000.jsonl"
        p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
        with self.assertRaises((EnvelopeError, MakerQueueError, SoakAuditError)):
            self.check()

    def test_bad_policy_rejected(self):
        for n in (-1, 0, 999, 180001, True, "3000"):
            with self.subTest(n=n), self.assertRaises((EnvelopeError, MakerQueueError)):
                self.check(n)
        for v in ("NaN", "-1", "1000", "inf"):
            with self.subTest(v=v), self.assertRaises(EnvelopeError):
                inspect(self.src, self.intents, self.assumptions, MakerPolicy(3000, v))

    def test_no_future_trade_can_change_quote_anchor(self):
        before = self.check()["results"][0]
        mf = json.loads((self.src / "manifest.json").read_text())
        p = self.src / mf["chunks"][1]["file"]
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        for row in rows:
            msg = json.loads(row["raw_text"])
            if msg["sequence_num"] == 6:
                msg["events"][0]["trades"][0]["size"] = "0.01"
                row["raw_text"] = json.dumps(msg, separators=(",", ":"))
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self._seal()
        after = self.check()["results"][0]
        self.assertEqual(before["anchor_sequence"], after["anchor_sequence"])
        self.assertEqual(
            before["posted_price_hypothetical"], after["posted_price_hypothetical"]
        )
        self.assertNotEqual(
            before["eligible_trade_volume_any_side_exact_price"],
            after["eligible_trade_volume_any_side_exact_price"],
        )

    def _edit_trade(self, mutation):
        mf = json.loads((self.src / "manifest.json").read_text())
        p = self.src / mf["chunks"][1]["file"]
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        for row in rows:
            msg = json.loads(row["raw_text"])
            if msg["sequence_num"] == 6:
                mutation(msg)
                row["raw_text"] = json.dumps(msg, separators=(",", ":"))
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self._seal()

    def test_snapshot_trade_not_new_execution(self):
        self._edit_trade(lambda msg: msg["events"][0].update(type="snapshot"))
        r = self.check()
        self.assertEqual(
            r["results"][0]["eligible_trade_volume_any_side_exact_price"], "0"
        )
        self.assertEqual(r["snapshot_trades_excluded"], 1)

    def test_trade_timestamp_before_hypothetical_anchor_is_rejected(self):
        def mutate(msg):
            msg["events"][0]["trades"][0]["time"] = "2020-01-01T00:00:00Z"

        self._edit_trade(mutate)
        r = self.check()
        self.assertEqual(
            r["results"][0]["eligible_trade_volume_any_side_exact_price"], "0"
        )
        self.assertEqual(
            r["results"][0]["pre_anchor_exchange_time_trade_exclusions"], 1
        )

    def test_independent_software_replay_detects_result_tamper(self):
        from independent_v19_trade_replay import verify

        report = self.check()
        self.assertEqual(
            verify(self.src, report)["candidates_verified"], ["fixture_maker"]
        )
        report["results"][0]["eligible_trade_volume_any_side_exact_price"] = "999"
        with self.assertRaisesRegex(ValueError, "mismatched"):
            verify(self.src, report)

    def test_replayed_identical_trade_id_rejected_by_source_admission(self):
        def duplicate(msg):
            trade = msg["events"][0]["trades"][0]
            msg["events"][0]["trades"].append(dict(trade))

        self._edit_trade(duplicate)
        with self.assertRaises((MakerQueueError, SoakAuditError, EnvelopeError)):
            self.check()

    def test_conflicting_trade_id_fails_closed(self):
        def conflicting(msg):
            trade = msg["events"][0]["trades"][0]
            msg["events"][0]["trades"].append(dict(trade, size="0.9"))

        self._edit_trade(conflicting)
        with self.assertRaises((MakerQueueError, SoakAuditError, EnvelopeError)):
            self.check()


if __name__ == "__main__":
    unittest.main()
