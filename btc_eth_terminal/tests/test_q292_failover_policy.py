"""Synthetic failover policy contracts, not live collector acceptance."""
import unittest
from dataclasses import replace

from q292_failover_policy import Arbiter, Source


def source(name, tick, session=None):
    marker = "a" if name == "primary" else "b"
    return Source(name, session or marker * 32, tick, 1,
                  True, True, True, True, marker * 64)


class FailoverContracts(unittest.TestCase):
    def setUp(self):
        self.a = Arbiter(confirmations=3)

    def prime(self):
        for tick in (10, 20, 30):
            result = self.a.update(tick, source("primary", tick),
                                   source("secondary", tick))
        self.assertEqual(result["active_source"], "primary")
        return result

    def test_independent_hot_standby_switch_revokes_cursor(self):
        old = self.prime()
        r = self.a.update(40, None, source("secondary", 40))
        self.assertEqual(r["active_source"], "secondary")
        self.assertTrue(r["revoked"])
        self.assertFalse(self.a.cursor_valid(old["generation"], old["source_session"]))
        self.assertTrue(self.a.cursor_valid(r["generation"], r["source_session"]))
        self.assertFalse(r["zero_gap_certified"])
        self.assertFalse(r["historical_gap_recovered"])
        for tick in (50, 60, 70):
            r = self.a.update(tick, source("primary", tick),
                              source("secondary", tick))
        self.assertEqual(r["active_source"], "secondary")

    def test_simultaneous_outage_and_old_epoch_rejected(self):
        old = self.prime()
        r = self.a.update(40)
        self.assertEqual(r["status"], "QUARANTINED")
        self.assertFalse(self.a.cursor_valid(old["generation"], old["source_session"]))
        for tick in (50, 60, 70):
            r = self.a.update(tick, source("primary", tick))
        self.assertIsNone(r["active_source"])
        for tick in (80, 90, 100):
            r = self.a.update(tick, source("primary", tick, "c" * 32))
        self.assertEqual(r["active_source"], "primary")

    def test_partial_snapshot_or_missing_independent_check_never_promotes(self):
        for field in ("both_books_valid", "snapshot_session_bound",
                      "independent_book_check", "ready"):
            a = Arbiter()
            for tick in (10, 20, 30, 40):
                r = a.update(tick, replace(source("primary", tick), **{field: False}))
            self.assertIsNone(r["active_source"])

    def test_duplicate_observation_and_stale_receiver_cannot_promote(self):
        x = source("primary", 10)
        for tick in (10, 20, 30):
            r = self.a.update(tick, x)
        self.assertIsNone(r["active_source"])
        for tick in (40, 50, 60):
            r = self.a.update(tick, replace(source("primary", tick),
                                             receiver_age_ms=4000))
        self.assertIsNone(r["active_source"])

    def test_shared_provenance_and_divergence_revoke(self):
        old = self.prime()
        r = self.a.update(40, source("primary", 40),
                          replace(source("secondary", 40), provenance="a" * 64))
        self.assertIsNone(r["active_source"])
        self.assertFalse(self.a.cursor_valid(old["generation"], old["source_session"]))
        for tick in (50, 60, 70):
            r = self.a.update(tick, source("primary", tick, "c" * 32),
                              source("secondary", tick), disagreement=True)
        self.assertIsNone(r["active_source"])

    def test_clock_regression_and_orders_blocked(self):
        self.a.update(10)
        with self.assertRaises(ValueError):
            self.a.update(10)
        for tick in (20, 30, 40):
            r = self.a.update(tick, replace(source("primary", tick), orders_enabled=True))
        self.assertIsNone(r["active_source"])


if __name__ == "__main__":
    unittest.main()
