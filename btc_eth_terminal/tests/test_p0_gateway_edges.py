"""Fail-closed boundary tests for the read-only dual-source runtime."""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from q292_failover_policy import Arbiter, Source
from q292_resilient_gateway import (
    MAX_RETIRED_SOURCE_EPOCHS,
    ManagedSource,
    ResilientRoute,
)
from test_u1_standby_epoch_fence import FakeObserver


def witness(name, now, session=None):
    marker = "a" if name == "primary" else "b"
    return Source(name, session or marker * 32, now, 0,
                  True, True, True, True, marker * 64)


class GatewayEdges(unittest.TestCase):
    def test_negative_receiver_age_never_cancels_elapsed(self):
        now = 5_000_000_000
        invalid = replace(witness("primary", now - 2_000_000),
                          receiver_age_ms=-1)
        self.assertFalse(invalid.admissible(now, 3_000_000_000))

    def test_standby_repeated_reply_never_becomes_hot(self):
        a = Arbiter()
        for tick in (10, 20, 30, 40):
            a.update(tick, witness("primary", tick), witness("secondary", 10))
        self.assertEqual(a.qualified, {"primary"})
        r = a.update(50, None, witness("secondary", 10))
        self.assertIsNone(r["active_source"])
        self.assertEqual(a.qualified, set())
        for tick in (60, 70, 80):
            r = a.update(tick, None, witness("secondary", tick))
        self.assertEqual(r["active_source"], "secondary")

    def test_two_sources_lost_then_old_epoch_never_returns(self):
        a = Arbiter()
        for tick in (10, 20, 30):
            first = a.update(tick, witness("primary", tick),
                             witness("secondary", tick))
        lost = a.update(40)
        self.assertIsNone(lost["active_source"])
        self.assertFalse(a.cursor_valid(first["generation"], first["source_session"]))
        self.assertEqual(a.qualified, set())

    def make_source(self, name, root, **kwargs):
        s = ManagedSource(name, root / name, 45, True,
                          factory=FakeObserver, **kwargs)
        self.addCleanup(s.close)
        return s

    def test_stale_second_poll_cannot_certify_dual_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = self.make_source("primary", root)
            s = self.make_source("secondary", root)
            route = ResilientRoute(p, s)
            for _ in range(4):
                route.read()
            prior = p.worker.observation
            with patch.object(p.worker, "observation", side_effect=lambda: (
                replace(prior()[0], observed_ns=time.monotonic_ns()-4_000_000_000),
                prior()[1],
            )):
                body = route.read()
            self.assertEqual(body["active_source"], "secondary")
            self.assertEqual(set(body["checkpoint_comparison"].values()),
                             {"SOURCE_UNAVAILABLE"})

    def test_terminal_reason_overrides_present_stale_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.make_source("primary", Path(directory))
            self.assertIsNotNone(p.tick())
            old = p.admitted_session
            p.worker.last_reason = "COLLECTOR_OBSERVATION_SOCKET_CLOSED"
            self.assertIsNone(p.tick())
            self.assertTrue(p.retiring)
            self.assertIn(old, p.retired_sessions)

    def test_retired_epoch_bound_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.make_source("primary", Path(directory))
            p.retired_sessions = {
                f"{i:032x}" for i in range(MAX_RETIRED_SOURCE_EPOCHS)
            }
            self.assertIsNone(p.tick())
            self.assertEqual(p.attempts, 0)

    def test_restart_budget_zero_prevents_new_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.make_source("primary", Path(directory), max_restarts=0)
            self.assertIsNotNone(p.tick())
            p.worker.process.kill()
            self.assertIsNone(p.tick())
            p.next_start = 0
            self.assertIsNone(p.tick())
            self.assertEqual(p.attempts, 1)

    def test_expired_deadline_does_not_start_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.make_source("primary", Path(directory),
                                 deadline=time.monotonic()-1)
            self.assertIsNone(p.tick())
            self.assertEqual(p.attempts, 0)


if __name__ == "__main__":
    unittest.main()
