"""Synthetic-only state-machine and adversarial tests. Never empirical fills."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from v24_paper_oms import FixturePaperOMS
from v24_paper_risk import FIXTURE, FixtureQuote, PaperPolicy, PaperReject, dec


def q(asset="BTC-USD", *, t=1000, recv=None, bid=None, ask=None, **kw):
    if bid is None:
        bid = "100" if asset == "BTC-USD" else "50"
    if ask is None:
        ask = "101" if asset == "BTC-USD" else "51"
    return FixtureQuote(
        instrument=asset,
        bid=bid,
        ask=ask,
        received_ms=t if recv is None else recv,
        decision_ms=t,
        **kw,
    )


def two(t=1000, *, btc_bid="100", btc_ask="101", eth_bid="50", eth_ask="51"):
    return {
        "BTC-USD": q(t=t, bid=btc_bid, ask=btc_ask),
        "ETH-USD": q("ETH-USD", t=t, bid=eth_bid, ask=eth_ask),
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.policy = PaperPolicy()
        self.oms = FixturePaperOMS(self.path, self.policy)

    def arm(self):
        self.oms.mark("mark1", two())
        return self.oms.risk_mode(
            "arm1", "ARM", reason="Manual fixture testing only", at_ms=1000, manual=True
        )

    def buy(self, ident="order1", qty="2", px="100", t=1000):
        return self.oms.submit(
            "intent:" + ident,
            ident,
            instrument="BTC-USD",
            side="BUY",
            quantity=qty,
            limit_price=px,
            quote=q(t=t),
        )

    def ack(self, ident="order1", t=1001):
        return self.oms.lifecycle("ack:" + ident, "ACK", ident, at_ms=t)

    def fill(self, ident="order1", qty="1", px="99", t=1002, suffix="1"):
        return self.oms.lifecycle(
            "fill:" + ident + suffix, "FILL", ident, at_ms=t, quantity=qty, price=px
        )


class TestExactDecimal(unittest.TestCase):
    def test_01_accept_exact_decimal_strings(self):
        self.assertEqual(dec("0.00000001", "qty", positive=True), Decimal("0.00000001"))

    def test_02_reject_float_and_bool(self):
        for x in (0.1, True, None, float("nan"), float("inf")):
            with self.subTest(x=x), self.assertRaises(PaperReject):
                dec(x, "invalid")

    def test_03_reject_nonfinite_string(self):
        for x in ("NaN", "Infinity", "-Infinity", "abc"):
            with self.subTest(x=x), self.assertRaises(PaperReject):
                dec(x, "bad")

    def test_04_policy_invalid_bounds(self):
        with self.assertRaises(PaperReject):
            PaperPolicy(max_order_usd="0")
        with self.assertRaises(PaperReject):
            PaperPolicy(fee_bps="1001")
        with self.assertRaises(PaperReject):
            PaperPolicy(max_staleness_ms=True)

    def test_05_quotes_validate_origin_integrity_crossed(self):
        p = PaperPolicy()
        for quote in (
            q(origin="COINBASE_REAL"),
            q(integrity="GAPPED"),
            q(bid="101", ask="101"),
            q(bid="102", ask="101"),
        ):
            with self.subTest(quote=quote), self.assertRaises(PaperReject):
                quote.validate(p)

    def test_06_quote_reject_future_or_stale(self):
        for quote in (q(t=1000, recv=1001), q(t=10000, recv=1000)):
            with self.subTest(quote=quote), self.assertRaises(PaperReject):
                quote.validate(PaperPolicy())


class TestLifecycle(Base):
    def test_07_initial_is_observe_only_and_no_external_fills(self):
        s = self.oms.snapshot()
        self.assertEqual(s["mode"], "OBSERVE_ONLY")
        self.assertFalse(s["live_orders_possible"])
        self.assertFalse(s["actual_fills_verified"])
        self.assertEqual(s["event_count"], 1)

    def test_08_cannot_arm_before_dual_market_mark(self):
        with self.assertRaises(PaperReject):
            self.oms.risk_mode(
                "arm",
                "ARM",
                reason="I explicitly authorize fixture",
                at_ms=1000,
                manual=True,
            )

    def test_09_arm_requires_manual_permission(self):
        self.oms.mark("mark1", two())
        with self.assertRaises(PaperReject):
            self.oms.risk_mode(
                "arm", "ARM", reason="Not authorized manually", at_ms=1000, manual=False
            )

    def test_10_arm_stale_market_rejected(self):
        self.oms.mark("mark1", two())
        with self.assertRaises(PaperReject):
            self.oms.risk_mode(
                "arm",
                "ARM",
                reason="Attempt stale synthetic arm",
                at_ms=4000,
                manual=True,
            )

    def test_11_buy_ack_partial_full_and_reopen(self):
        self.arm()
        self.buy()
        self.ack()
        a = self.fill()
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "PARTIAL")
        a = self.fill(qty="1", px="100", t=1003, suffix="2")
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "FILLED")
        self.assertEqual(Decimal(a["positions"]["BTC-USD"]), Decimal("2"))
        self.assertEqual(
            Decimal(a["cash_usd"]), Decimal("10000") - Decimal("199") * Decimal("1.001")
        )
        b = FixturePaperOMS(self.path, self.policy).snapshot()
        self.assertEqual(a, b)

    def test_12_reopening_with_policy_change_refused(self):
        with self.assertRaises(PaperReject):
            FixturePaperOMS(self.path, PaperPolicy(max_gross_usd="8000"))

    def test_13_idempotent_ack_same_payload(self):
        self.arm()
        self.buy()
        a = self.ack()
        b = self.ack()
        self.assertEqual(a, b)
        self.assertEqual(b["event_count"], 5)

    def test_14_idempotent_event_conflict_rejected(self):
        self.arm()
        self.buy()
        self.ack()
        with self.assertRaises(PaperReject):
            self.oms.lifecycle(
                "ack:order1", "FILL", "order1", at_ms=1002, quantity="1", price="99"
            )

    def test_15_duplicate_client_order_rejected(self):
        self.arm()
        self.buy()
        with self.assertRaises(PaperReject):
            self.oms.submit(
                "intent:second",
                "order1",
                instrument="BTC-USD",
                side="BUY",
                quantity="2",
                limit_price="100",
                quote=q(),
            )

    def test_16_reject_fill_without_ack(self):
        self.arm()
        self.buy()
        with self.assertRaises(PaperReject):
            self.fill()

    def test_17_reject_overfill(self):
        self.arm()
        self.buy()
        self.ack()
        with self.assertRaises(PaperReject):
            self.fill(qty="3")

    def test_18_reject_wrong_limit_fill(self):
        self.arm()
        self.buy()
        self.ack()
        with self.assertRaises(PaperReject):
            self.fill(px="100.01")

    def test_19_reject_nonpositive_fill(self):
        self.arm()
        self.buy()
        self.ack()
        with self.assertRaises(PaperReject):
            self.fill(qty="0")

    def test_20_reject_inverted_event_clocks(self):
        self.arm()
        self.buy()
        self.ack()
        with self.assertRaises(PaperReject):
            self.fill(t=1000)

    def test_21_cancel_pending_race_fill_then_cancel_ack(self):
        self.arm()
        self.buy()
        self.ack()
        self.oms.lifecycle("cancelreq", "CANCEL_REQUEST", "order1", at_ms=1002)
        a = self.fill(qty="0.4", px="99", t=1003)
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "CANCEL_PENDING")
        a = self.oms.lifecycle("cancelack", "CANCEL_ACK", "order1", at_ms=1004)
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "CANCELLED")
        self.assertEqual(a["orders"]["PAPER:order1"]["filled_quantity"], "0.4")
        with self.assertRaises(PaperReject):
            self.fill(qty="0.1", px="99", t=1005, suffix="2")

    def test_22_full_fill_before_cancel_confirmation_preserved(self):
        self.arm()
        self.buy()
        self.ack()
        self.oms.lifecycle("cancelreq", "CANCEL_REQUEST", "order1", at_ms=1002)
        self.fill(qty="2", px="99", t=1003)
        a = self.oms.lifecycle("cancelack", "CANCEL_ACK", "order1", at_ms=1004)
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "FILLED")

    def test_23_cancel_before_ack(self):
        self.arm()
        self.buy()
        self.oms.lifecycle("cancelreq", "CANCEL_REQUEST", "order1", at_ms=1001)
        a = self.oms.lifecycle("cancelack", "CANCEL_ACK", "order1", at_ms=1002)
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "CANCELLED")
        with self.assertRaises(PaperReject):
            self.ack(t=1003)

    def test_24_reject_order_before_fill(self):
        self.arm()
        self.buy()
        a = self.oms.lifecycle(
            "reject1", "REJECT", "order1", at_ms=1001, reason="Synthetic reject fixture"
        )
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "REJECTED")
        self.assertEqual(a["reserved_cash_usd"], "0")

    def test_25_reject_order_after_partial_fill_impossible(self):
        self.arm()
        self.buy()
        self.ack()
        self.fill()
        with self.assertRaises(PaperReject):
            self.oms.lifecycle(
                "reject1", "REJECT", "order1", at_ms=1003, reason="late reject"
            )

    def test_26_halt_blocks_new_orders_but_allows_inflight_reconciliation(self):
        self.arm()
        self.buy()
        self.ack()
        s = self.oms.risk_mode(
            "halt1",
            "HALT",
            reason="Synthetic source deliberately degraded",
            at_ms=1001,
            manual=False,
        )
        self.assertEqual(s["mode"], "HALT")
        s = self.fill(t=1002)
        self.assertEqual(s["orders"]["PAPER:order1"]["status"], "PARTIAL")
        s = self.buy("afterhalt")
        self.assertEqual(s["orders"]["PAPER:afterhalt"]["status"], "DENIED")

    def test_27_halt_recovery_requires_manual_and_rearm(self):
        self.arm()
        self.oms.risk_mode(
            "halt1",
            "HALT",
            reason="Manually triggered emergency stop",
            at_ms=1001,
            manual=True,
        )
        with self.assertRaises(PaperReject):
            self.oms.risk_mode(
                "rec",
                "RECOVER",
                reason="Automated recovery not allowed",
                at_ms=1002,
                manual=False,
            )
        s = self.oms.risk_mode(
            "rec",
            "RECOVER",
            reason="Operator checked synthetic source",
            at_ms=1002,
            manual=True,
        )
        self.assertEqual(s["mode"], "OBSERVE_ONLY")
        self.oms.mark("mark2", two(1003))
        s = self.oms.risk_mode(
            "arm2",
            "ARM",
            reason="Operator explicitly rearmed fixture",
            at_ms=1003,
            manual=True,
        )
        self.assertEqual(s["mode"], "NORMAL")

    def test_28_risk_degrade_requires_rearm(self):
        self.arm()
        s = self.oms.risk_mode(
            "deg1",
            "DEGRADE",
            reason="Fixture health questionable",
            at_ms=1001,
            manual=False,
        )
        self.assertEqual(s["mode"], "OBSERVE_ONLY")
        self.assertEqual(self.buy()["orders"]["PAPER:order1"]["status"], "DENIED")

    def test_29_mark_drawdown_halts(self):
        self.arm()
        self.buy(qty="3", px="100")
        self.ack()
        self.fill(qty="3", px="99")
        # Mark loss from 99 to 1 on three units: > 250 USD.
        s = self.oms.mark("mark2", two(1003, btc_bid="1", btc_ask="2"))
        self.assertEqual(s["mode"], "HALT")
        self.assertGreater(Decimal(s["drawdown_usd"]), Decimal("250"))

    def test_30_mark_clock_regression_rejected(self):
        self.arm()
        with self.assertRaises(PaperReject):
            self.oms.mark("markpast", two(999))

    def test_31_unhealthy_or_wrong_quote_blocked(self):
        self.arm()
        for idx, bad in enumerate(
            (
                q(integrity="GAPPED"),
                q(origin="REAL_COINBASE"),
                q(recv=1001),
                q(bid="101", ask="100"),
            )
        ):
            s = self.oms.submit(
                f"intent:bad{idx}",
                f"bad{idx}",
                instrument="BTC-USD",
                side="BUY",
                quantity="1",
                limit_price="100",
                quote=bad,
            )
            self.assertEqual(s["orders"][f"PAPER:bad{idx}"]["status"], "DENIED")

    def test_32_stale_market_marks_block_order(self):
        self.arm()
        s = self.buy("late", t=4000)
        self.assertEqual(s["orders"]["PAPER:late"]["status"], "DENIED")

    def test_33_no_shorts_on_spot(self):
        self.arm()
        s = self.oms.submit(
            "sell1",
            "sell1",
            instrument="BTC-USD",
            side="SELL",
            quantity="1",
            limit_price="101",
            quote=q(),
        )
        self.assertEqual(s["orders"]["PAPER:sell1"]["status"], "DENIED")

    def test_34_sell_after_buy_inventory_conserves(self):
        self.arm()
        self.buy()
        self.ack()
        self.fill(qty="2", px="99")
        s = self.oms.submit(
            "sell1",
            "sell1",
            instrument="BTC-USD",
            side="SELL",
            quantity="1",
            limit_price="100",
            quote=q(),
        )
        self.assertEqual(s["orders"]["PAPER:sell1"]["status"], "ACCEPTED")
        self.oms.lifecycle("ack:sell", "ACK", "sell1", at_ms=1003)
        s = self.oms.lifecycle(
            "fill:sell", "FILL", "sell1", at_ms=1004, quantity="1", price="101"
        )
        self.assertEqual(Decimal(s["positions"]["BTC-USD"]), Decimal("1"))
        self.assertEqual(
            Decimal(s["cash_usd"]),
            Decimal("10000")
            - Decimal("198") * Decimal("1.001")
            + Decimal("101") * Decimal("0.999"),
        )

    def test_35_max_order_notional_denied(self):
        self.arm()
        self.assertEqual(
            self.buy(qty="20")["orders"]["PAPER:order1"]["status"], "DENIED"
        )

    def test_36_open_buy_reservations_prevent_double_spend(self):
        self.policy = PaperPolicy(
            starting_cash_usd="200",
            max_order_usd="200",
            max_symbol_usd="1000",
            max_gross_usd="2000",
        )
        self.path.unlink()
        self.oms = FixturePaperOMS(self.path, self.policy)
        self.arm()
        self.buy("a", qty="1", px="100")
        second = self.buy("b", qty="1.1", px="100")
        self.assertEqual(second["orders"]["PAPER:b"]["status"], "DENIED")

    def test_37_sell_inventory_cannot_double_reserve(self):
        self.arm()
        self.buy()
        self.ack()
        self.fill(qty="2", px="99")
        self.oms.submit(
            "sell1",
            "sell1",
            instrument="BTC-USD",
            side="SELL",
            quantity="2",
            limit_price="100",
            quote=q(),
        )
        s = self.oms.submit(
            "sell2",
            "sell2",
            instrument="BTC-USD",
            side="SELL",
            quantity="1",
            limit_price="100",
            quote=q(),
        )
        self.assertEqual(s["orders"]["PAPER:sell2"]["status"], "DENIED")

    def test_38_mark_from_external_origin_fails(self):
        with self.assertRaises(PaperReject):
            self.oms.mark(
                "mark1", {"BTC-USD": q(origin="REAL_SOURCE"), "ETH-USD": q("ETH-USD")}
            )

    def test_39_bad_order_id_fails(self):
        self.arm()
        with self.assertRaises(PaperReject):
            self.buy("../evil")

    def test_40_event_tamper_detected_on_replay(self):
        self.arm()
        self.buy()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE events SET payload=? WHERE seq=3", ("{}",))
        with self.assertRaises(PaperReject):
            self.oms.snapshot()

    def test_41_removed_event_detected_on_replay(self):
        self.arm()
        self.buy()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("DELETE FROM events WHERE seq=2")
        with self.assertRaises(PaperReject):
            self.oms.snapshot()

    def test_42_wrong_risk_policy_is_detected(self):
        self.arm()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with self.assertRaises(PaperReject):
            FixturePaperOMS(self.path, self.policy)

    def test_43_symlink_ledger_path_refused(self):
        alt = Path(self.tmp.name) / "link.sqlite"
        alt.symlink_to(self.path)
        with self.assertRaises(PaperReject):
            FixturePaperOMS(alt)

    def test_44_crash_before_transaction_commit_preserves_previous_state(self):
        self.arm()
        before = self.oms.snapshot()
        cmd = (
            "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute(\"INSERT INTO events VALUES "
            "(99,'bad','ACK','PAPER:bad','{}','x','x')\"); "
            "os._exit(12)"
        )
        res = subprocess.run(
            [sys.executable, "-c", cmd, str(self.path)], check=False, timeout=5
        )
        self.assertEqual(res.returncode, 12)
        self.assertEqual(self.oms.snapshot(), before)

    def test_45_parallel_reservations_are_transactionally_serialized(self):
        self.policy = PaperPolicy(
            starting_cash_usd="150",
            max_order_usd="200",
            max_symbol_usd="1000",
            max_gross_usd="2000",
        )
        self.path.unlink()
        self.oms = FixturePaperOMS(self.path, self.policy)
        self.arm()
        results = []

        def worker(i):
            x = self.oms.submit(
                f"req{i}",
                f"o{i}",
                instrument="BTC-USD",
                side="BUY",
                quantity="1",
                limit_price="100",
                quote=q(),
            )
            results.append(x["orders"][f"PAPER:o{i}"]["status"])

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5)
        self.assertEqual(sorted(results), ["ACCEPTED", "DENIED"])

    def test_46_mark_pnl_is_simulated_not_external_fill(self):
        self.arm()
        self.buy()
        self.ack()
        self.fill(qty="2", px="99")
        s = self.oms.mark("mark2", two(1003, btc_bid="102", btc_ask="103"))
        self.assertFalse(s["actual_fills_verified"])
        self.assertEqual(s["source_type"], FIXTURE)
        self.assertEqual(s["mode"], "NORMAL")

    def test_47_empty_position_sell_rejection_is_idempotent(self):
        self.arm()
        kw = dict(
            instrument="BTC-USD",
            side="SELL",
            quantity="1",
            limit_price="101",
            quote=q(),
        )
        x = self.oms.submit("deny1", "empty", **kw)
        y = self.oms.submit("deny1", "empty", **kw)
        self.assertEqual(x, y)

    def test_48_repeating_accepted_intent_after_halt_is_idempotent(self):
        self.arm()
        self.buy()
        self.oms.risk_mode(
            "halt1",
            "HALT",
            reason="Synthetic source integrity failure",
            at_ms=1001,
            manual=False,
        )
        # Retries cannot be reclassified as denied or issue a second order.
        s = self.buy()
        self.assertEqual(s["orders"]["PAPER:order1"]["status"], "ACCEPTED")
        self.assertEqual(s["event_count"], 5)

    def test_49_repeating_denied_intent_after_recovery_keeps_denied(self):
        self.oms.mark("m", two())
        s = self.buy("denied")
        self.assertEqual(s["orders"]["PAPER:denied"]["status"], "DENIED")
        self.oms.risk_mode(
            "armed",
            "ARM",
            reason="Explicit synthetic fixture authorization",
            at_ms=1000,
            manual=True,
        )
        s = self.buy("denied")
        self.assertEqual(s["orders"]["PAPER:denied"]["status"], "DENIED")

    def test_50_exposure_high_limit_not_hidden_by_low_quote(self):
        self.policy = PaperPolicy(
            max_order_usd="1000", max_symbol_usd="220", max_gross_usd="1500"
        )
        self.path.unlink()
        self.oms = FixturePaperOMS(self.path, self.policy)
        self.arm()
        s = self.buy("high", qty="2", px="150")
        self.assertEqual(s["orders"]["PAPER:high"]["status"], "DENIED")

    def test_51_full_cancel_pending_survives_restart(self):
        self.arm()
        self.buy()
        self.ack()
        self.oms.lifecycle("reqcan", "CANCEL_REQUEST", "order1", at_ms=1002)
        new = FixturePaperOMS(self.path, self.policy)
        self.assertEqual(
            new.snapshot()["orders"]["PAPER:order1"]["status"], "CANCEL_PENDING"
        )
        s = new.lifecycle("ackcan", "CANCEL_ACK", "order1", at_ms=1003)
        self.assertEqual(s["orders"]["PAPER:order1"]["status"], "CANCELLED")

    def test_52_fixture_event_capacity_fails_closed(self):
        from unittest.mock import patch

        import v24_paper_oms

        self.arm()
        with patch.object(v24_paper_oms, "MAX_FIXTURE_EVENTS", 3):
            with self.assertRaises(PaperReject):
                self.buy()
        self.assertEqual(self.oms.snapshot()["event_count"], 3)

    def test_53_independent_audit_matches_primary_engine(self):
        from v24_independent_fixture_audit import audit_fixture

        self.arm()
        self.buy()
        self.ack()
        self.fill()
        primary = self.oms.snapshot()
        second = audit_fixture(self.path)
        self.assertEqual(primary["cash_usd"], second["cash_usd"])
        self.assertEqual(primary["positions"], second["positions"])
        self.assertEqual(primary["event_count"], second["event_count"])
        self.assertEqual(
            primary["unanchored_local_event_chain_sha256"],
            second["same_original_event_chain_sha256"],
        )

    def test_54_independent_audit_detects_event_tampering(self):
        from v24_independent_fixture_audit import IndependentAuditFailure, audit_fixture

        self.arm()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE events SET payload='{}' WHERE seq=2")
        with self.assertRaises(IndependentAuditFailure):
            audit_fixture(self.path)

    def test_55_external_fixture_cannot_be_relabelled_real(self):
        from v24_independent_fixture_audit import audit_fixture

        self.arm()
        a = audit_fixture(self.path)
        self.assertFalse(a["independent_exchange_truth_verified"])
        self.assertFalse(a["preregistered_oos_alpha_verified"])
        self.assertEqual(a["actual_exchange_orders"], 0)

    def test_56_immediate_fill_drawdown_halts_without_waiting_for_mark(self):
        self.arm()
        self.buy("expensive", qty="1", px="1000")
        self.ack("expensive")
        a = self.fill("expensive", qty="1", px="1000")
        self.assertEqual(a["mode"], "HALT")
        self.assertGreater(Decimal(a["drawdown_usd"]), Decimal("250"))

    def test_57_late_fill_with_expired_mark_forces_halt(self):
        self.arm()
        self.buy()
        self.ack()
        a = self.fill(t=9000)
        self.assertEqual(a["mode"], "HALT")
        self.assertEqual(a["orders"]["PAPER:order1"]["status"], "PARTIAL")

    def test_58_nonmonotonic_risk_mode_clock_rejected(self):
        self.arm()
        self.oms.risk_mode(
            "halt1",
            "HALT",
            reason="Synthetic integrity failure observed",
            at_ms=1002,
            manual=False,
        )
        with self.assertRaises(PaperReject):
            self.oms.risk_mode(
                "recover1",
                "RECOVER",
                reason="Synthetic operator attempts reversal",
                at_ms=1001,
                manual=True,
            )

    def test_59_mutated_quote_after_order_receipt_never_changes_old_state(self):
        self.arm()
        before = self.buy()
        # No mutable reference survives in the ledger; future synthetic prices
        # are distinct MARK events, not retroactive changes to the INTENT.
        self.oms.mark("mark2", two(1001, btc_bid="98", btc_ask="99"))
        after = self.oms.snapshot()
        self.assertEqual(
            before["orders"]["PAPER:order1"], after["orders"]["PAPER:order1"]
        )


if __name__ == "__main__":
    unittest.main()
