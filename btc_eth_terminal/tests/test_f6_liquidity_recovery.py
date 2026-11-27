import unittest
from f6_liquidity_recovery import project, POLICY, RecoverySignal
from test_f4_ofi_markout import packet
from decimal import Decimal
import copy


class Recovery(unittest.TestCase):
    def test_state_machine_and_frozen_anchor(self):
        p = copy.deepcopy(POLICY)
        p["warmup_samples"] = 2
        s = RecoverySignal(p)
        self.assertFalse(s.step(Decimal(10), Decimal(1)))
        self.assertFalse(s.step(Decimal(10), Decimal(1)))
        self.assertFalse(s.step(Decimal(4), Decimal(1)))
        self.assertTrue(s.step(Decimal(8), Decimal(1)))

    def test_event_driven_roundtrip(self):
        p = copy.deepcopy(POLICY)
        p.update(
            warmup_samples=2,
            step_ms=100,
            hold_ms=100,
            latency_ms=10,
            tolerance_ms=100,
            max_age_ms=1000,
            fee_bps="0",
            extra_slippage_bps="0",
            capacity_fraction="1",
        )
        rows = [
            packet(0, 1000, "snapshot", [("bid", "99", "10"), ("offer", "101", "1")])
        ]
        for t, q in [
            (1050, "10"),
            (1150, "10"),
            (1250, "4"),
            (1350, "8"),
            (1450, "8"),
            (1550, "8"),
            (1650, "8"),
        ]:
            rows.append(packet(len(rows), t, "update", [("bid", "99", q)]))
        a = project(rows, "BTC-USD", p)
        self.assertEqual(a["signals"], 1)
        self.assertEqual(a["closed_roundtrips"], 1)
        self.assertEqual(Decimal(a["realized_net_quote"]), Decimal("-0.02"))
        self.assertFalse(a["unresolved_position"])
        self.assertFalse(a["alpha_confirmed"])

    def test_unresolved_exit_never_flat(self):
        p = copy.deepcopy(POLICY)
        p.update(
            warmup_samples=2,
            step_ms=100,
            hold_ms=500,
            latency_ms=10,
            tolerance_ms=100,
            max_age_ms=1000,
            fee_bps="0",
            extra_slippage_bps="0",
            capacity_fraction="1",
        )
        rows = [
            packet(0, 1000, "snapshot", [("bid", "99", "10"), ("offer", "101", "1")])
        ]
        for t, q in [(1050, "10"), (1150, "10"), (1250, "4"), (1350, "8"), (1450, "8")]:
            rows.append(packet(len(rows), t, "update", [("bid", "99", q)]))
        r = project(rows, "BTC-USD", p)
        self.assertEqual(r["entries"], 1)
        self.assertTrue(r["unresolved_position"])
        self.assertIsNone(r["final_cash_if_flat"])
