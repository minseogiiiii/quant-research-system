"""v15 read-only conditional quote envelope tests; no synthetic PnL / orders."""

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
    from taker_quote_envelope import Assumptions, EnvelopeError, evaluate
    from test_v10_soak import create_archive

    return Assumptions, EnvelopeError, evaluate, create_archive


Assumptions, EnvelopeError, evaluate, create_archive = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class TakerEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.src = Path(self.t.name) / "synthetic"
        create_archive(self.src)
        mf = json.loads((self.src / "manifest.json").read_text())
        # Toy receiver clock advances 1s between messages, unlike the original
        # test fixture whose seven packets share the same receive timestamp.
        for c in mf["chunks"]:
            f = self.src / c["file"]
            rows = []
            for line in f.read_text().splitlines():
                rec = json.loads(line)
                seq = json.loads(rec["raw_text"])["sequence_num"]
                rec["local_monotonic_ns"] = str((seq + 1) * 1_000_000_000)
                rows.append(json.dumps(rec, separators=(",", ":")) + "\n")
            payload = "".join(rows).encode()
            f.write_bytes(payload)
            c["archive_sha256"] = sha256(payload).hexdigest()
            c["archived_line_bytes"] = len(payload)
        (self.src / "manifest.json").write_text(json.dumps(mf))
        self.params = Assumptions(1000, 4000, 3000, "10", "5", "0.5")

    def intent(self, symbol="BTC-USD", seq=2, side="BUY", qty="1", **kwargs):
        i = {
            "id": "a",
            "product_id": symbol,
            "side": side,
            "quantity": qty,
            "signal_sequence": seq,
        }
        i.update(kwargs)
        return [i]

    def test_first_later_product_update_only_and_explicit_economics(self):
        d = evaluate(self.src, self.intent(), self.params)
        q = d["results"][0]
        self.assertEqual(q["status"], "INDICATIVE_TAKER_QUOTE_ONLY")
        self.assertEqual(q["signal_sequence"], 2)
        self.assertEqual(q["quote_sequence"], 3)
        self.assertEqual(q["market_best_ask"], "101")
        self.assertEqual(q["hypothetical_quoted_price"], "101.0505")
        self.assertEqual(q["hypothetical_one_way_fee_usd"], "0.1010505")
        self.assertEqual(q["hypothetical_cashflow_usd"], "-101.1515505")
        self.assertFalse(d["fills_verified"])
        self.assertFalse(d["trading_or_api_writes_enabled"])

    def test_eth_does_not_price_on_btc_update(self):
        d = evaluate(self.src, self.intent(symbol="ETH-USD", qty="0.2"), self.params)
        self.assertEqual(d["results"][0]["quote_sequence"], 4)
        self.assertEqual(d["results"][0]["market_best_ask"], "202")

    def test_sell_uses_bid_not_mid_or_ask(self):
        d = evaluate(self.src, self.intent(side="SELL"), self.params)
        q = d["results"][0]
        self.assertEqual(q["market_best_bid"], "100")
        self.assertEqual(q["hypothetical_quoted_price"], "99.9500")
        self.assertGreater(float(q["hypothetical_cashflow_usd"]), 0)

    def test_cannot_claim_best_level_capacity_above_haircut(self):
        d = evaluate(self.src, self.intent(qty="1.1"), self.params)
        self.assertEqual(
            d["results"][0]["status"], "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH"
        )
        self.assertNotIn("hypothetical_cashflow_usd", d["results"][0])

    def test_no_target_price_after_required_delay_is_rejected(self):
        p = Assumptions(2500, 4000, 3000, "10", "5", "0.5")
        d = evaluate(self.src, self.intent(), p)
        self.assertEqual(d["results"][0]["status"], "REJECTED_NO_TIMELY_PRODUCT_QUOTE")

    def test_signal_before_both_snapshots_rejected(self):
        d = evaluate(self.src, self.intent(seq=1), self.params)
        self.assertEqual(d["results"][0]["status"], "REJECTED_NOT_READY_AT_SIGNAL")

    def test_does_not_use_the_same_event_as_its_own_execution_quote(self):
        # BTC book changes at seq3. An intent created *at* seq3 cannot
        # retroactively execute on that change, and ETH seq4 cannot price BTC.
        d = evaluate(self.src, self.intent(seq=3), self.params)
        self.assertEqual(d["results"][0]["status"], "REJECTED_NO_TIMELY_PRODUCT_QUOTE")

    def test_stale_signal_book_is_rejected(self):
        # At seq6 the toy BTC book has not changed since seq3; after a
        # 2.5-second max age it is not eligible as a decision reference.
        p = Assumptions(1000, 4000, 2500, "10", "5", "0.5")
        d = evaluate(self.src, self.intent(seq=6), p)
        self.assertEqual(d["results"][0]["status"], "REJECTED_STALE_SIGNAL_BOOK")

    def test_unknown_sequence_rejected_not_cherrypicked(self):
        d = evaluate(self.src, self.intent(seq=700), self.params)
        self.assertEqual(
            d["results"][0]["status"], "REJECTED_SIGNAL_SEQUENCE_NOT_IN_SOURCE"
        )

    def test_bad_inputs_fail_closed(self):
        for bad in ("NaN", "Infinity", "-1", "1000", "1e100"):
            with self.subTest(fee=bad), self.assertRaises(EnvelopeError):
                evaluate(
                    self.src,
                    self.intent(),
                    Assumptions(1000, 4000, 3000, bad, "0", "0.5"),
                )
        for bad in ("NaN", "-1", "1e100", "0"):
            with self.subTest(qty=bad), self.assertRaises(EnvelopeError):
                evaluate(self.src, self.intent(qty=bad), self.params)
        for bad in (0, 60001, True):
            with self.subTest(latency=bad), self.assertRaises(EnvelopeError):
                evaluate(
                    self.src,
                    self.intent(),
                    Assumptions(bad, 65000, 3000, "1", "0", "0.5"),
                )
        with self.assertRaises(EnvelopeError):
            evaluate(self.src, self.intent(credentials="do-not-read"), self.params)

    def test_duplicate_request_id_cannot_aggregate_results(self):
        with self.assertRaises(EnvelopeError):
            evaluate(self.src, self.intent() + self.intent(seq=4), self.params)

    def test_gapped_real_style_archive_not_admitted(self):
        mf = json.loads((self.src / "manifest.json").read_text())
        mf["status"] = "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
        mf["ready_for_research"] = False
        mf["faults"] = [{"connection_id": 0, "reason": "DISCONNECT"}]
        (self.src / "manifest.json").write_text(json.dumps(mf))
        with self.assertRaises(EnvelopeError):
            evaluate(self.src, self.intent(), self.params)

    def test_source_mutated_after_admission_still_rejected(self):
        from taker_quote_envelope import audit as audit_actual

        def mutate_after_audit(root):
            a = audit_actual(root)
            p = Path(root) / "chunks/chunk_00001_conn_000.jsonl"
            p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
            return a

        with patch("taker_quote_envelope.audit", side_effect=mutate_after_audit):
            with self.assertRaisesRegex(EnvelopeError, "changed after admission"):
                evaluate(self.src, self.intent(), self.params)

    def test_zero_assumed_fee_requires_explicit_zero(self):
        p = Assumptions(1000, 4000, 3000, "0", "0", "1")
        r = evaluate(self.src, self.intent(), p)
        self.assertEqual(r["one_way_fee_bps_explicit_unverified"], "0")
        self.assertFalse(r["historic_venue_fee_tier_verified"])
        self.assertEqual(r["results"][0]["hypothetical_quoted_price"], "101")


if __name__ == "__main__":
    unittest.main()
