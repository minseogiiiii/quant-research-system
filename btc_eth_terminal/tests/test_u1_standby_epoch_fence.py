"""Deterministic dual-source gap and epoch fencing tests, no live market I/O."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from q292_failover_policy import Source
from q292_resilient_gateway import (
    EXPLICIT_SOURCE_GAP,
    ManagedSource,
    ResilientRoute,
)


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def kill(self):
        self.code = -9


class FakeObserver:
    def __init__(self, name, work, duration, synthetic):
        self.name = name
        self.session = ("a" if name == "primary" else "b") * 32
        self.provenance = ("1" if name == "primary" else "2") * 64
        self.process = FakeProcess()
        self.failed = False
        self.last_reason = "READY"

    def observation(self):
        if self.session is None:
            return None
        source = Source(
            self.name, self.session, time.monotonic_ns(), 0,
            True, True, True, True, self.provenance,
        )
        return source, {
            p: {"source_raw_sha256": self.provenance}
            for p in ("BTC-USD", "ETH-USD")
        }

    def close(self):
        self.process.kill()


class StandbyEpochFence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.primary = ManagedSource("primary", root / "p", 180, True,
                                     factory=FakeObserver)
        self.secondary = ManagedSource("secondary", root / "s", 180, True,
                                       factory=FakeObserver)
        self.addCleanup(self.primary.close)
        self.addCleanup(self.secondary.close)
        self.route = ResilientRoute(self.primary, self.secondary)
        for _ in range(4):
            self.initial = self.route.read()
        self.assertEqual(self.initial["active_source"], "primary")

    def test_standby_recovers_only_on_new_epoch(self):
        worker = self.secondary.worker
        old = worker.session
        worker.session = None
        worker.last_reason = EXPLICIT_SOURCE_GAP
        lost = self.route.read()
        self.assertEqual(lost["active_source"], "primary")
        self.assertEqual(set(lost["checkpoint_comparison"].values()),
                         {"SOURCE_UNAVAILABLE"})
        self.assertIn(old, self.secondary.retired_sessions)
        worker.session = "c" * 32
        worker.last_reason = "READY"
        recovered = self.route.read()
        self.assertEqual(recovered["generation"], self.initial["generation"])
        self.assertEqual(set(recovered["checkpoint_comparison"].values()),
                         {"NOT_COMPARABLE"})
        self.assertEqual(self.secondary.attempts, 1)
        self.assertFalse(recovered["historical_gap_recovered"])
        self.assertFalse(recovered["zero_gap_certified"])
        worker.session = old
        rejected = self.route.read()
        self.assertEqual(rejected["active_source"], "primary")
        self.assertTrue(self.secondary.retiring)
        self.assertEqual(set(rejected["checkpoint_comparison"].values()),
                         {"SOURCE_UNAVAILABLE"})

    def test_gap_reason_overrides_stale_observation(self):
        worker = self.secondary.worker
        old = worker.session
        worker.last_reason = EXPLICIT_SOURCE_GAP
        self.route.read()
        self.assertIn(old, self.secondary.retired_sessions)
        worker.last_reason = "READY"
        result = self.route.read()
        self.assertTrue(self.secondary.retiring)
        self.assertEqual(result["active_source"], "primary")

    def test_active_gap_revokes_old_cursor_and_never_stitches(self):
        worker = self.primary.worker
        old = worker.session
        worker.session = None
        worker.last_reason = EXPLICIT_SOURCE_GAP
        result = self.route.read()
        self.assertEqual(result["active_source"], "secondary")
        self.assertGreater(result["generation"], self.initial["generation"])
        self.assertFalse(self.route.arbiter.cursor_valid(
            self.initial["generation"], old))
        worker.session = old
        worker.last_reason = "READY"
        self.route.read()
        self.assertTrue(self.primary.retiring)

    def test_epoch_rotation_without_observed_gap_fences_previous(self):
        worker = self.secondary.worker
        old = worker.session
        worker.session = "c" * 32
        self.route.read()
        self.assertIn(old, self.secondary.retired_sessions)
        worker.session = old
        self.route.read()
        self.assertTrue(self.secondary.retiring)


if __name__ == "__main__":
    unittest.main()
