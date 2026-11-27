"""C2 operational acceptance policy, independent of collector and wall-clock I/O.

C1 remains the strict clean baseline. This contract never certifies continuity
through a gap. Time is conservatively charged to degradation if either sample
endpoint is degraded. Every published epoch must be fresh and monotone.
"""
from __future__ import annotations

import math

from q292_dual_gateway import PRODUCTS, cursor
from q293_dual_1800s import need

MAX_STARTUP_SECONDS = 20
MAX_POLL_GAP_SECONDS = 5
MAX_OUTAGE_SECONDS = 5
MAX_RECOVERY_SECONDS = 20
MAX_INCIDENTS = 8
MAX_TOTAL_OUTAGE_SECONDS = 20
MAX_TOTAL_DEGRADED_SECONDS = 120


class RecoveryMonitor:
    def __init__(self, duration):
        need(type(duration) is int and duration in (90, 1800, 18000),
             "C2_DURATION")
        self.duration = duration
        self.last = None
        self.started = None
        self.generation = -1
        self.run = None
        self.current_cursor = None
        self.retired = set()
        self.previous_health = (False, False)
        self.degraded_since = None
        self.outage_since = None
        self.degraded_seconds = 0.0
        self.outage_seconds = 0.0
        self.incidents = []
        self.samples = 0
        self.both_samples = 0
        self.max_poll = 0.0
        self.attempts = dict.fromkeys(("primary", "secondary"), 0)

    def observe(self, elapsed, body):
        need(type(elapsed) in (int, float) and math.isfinite(elapsed)
             and elapsed >= 0 and (self.last is None or elapsed > self.last),
             "C2_CLOCK")
        dt = 0.0 if self.last is None else elapsed - self.last
        need(dt <= MAX_POLL_GAP_SECONDS, "C2_POLL_GAP")
        self.max_poll = max(self.max_poll, dt)
        self.samples += 1
        self.last = elapsed
        need(body.get("real_orders_enabled") is False
             and body.get("zero_gap_certified") is False
             and body.get("historical_gap_recovered", False) is False
             and body.get("execution_authorized", False) is False
             and body.get("exchange_attested", False) is False, "C2_AUTHORITY")
        generation = body["generation"]
        run = body["gateway_run"]
        if self.run is None:
            self.run = run
        need(run == self.run and type(generation) is int
             and generation >= max(0, self.generation), "C2_GENERATION")
        self.generation = generation
        ready = body["states"] is not None
        checks = body.get("checkpoint_comparison", {})
        need("DIVERGENCE" not in checks.values(), "C2_CROSS_SOURCE_DIVERGENCE")
        both = ready and set(checks) == set(PRODUCTS) and (
            set(checks.values()) <= {"MATCH", "NOT_COMPARABLE"}
        )
        attempts = body.get("source_attempts", self.attempts)
        need(set(attempts) == set(self.attempts)
             and all(type(v) is int and self.attempts[k] <= v <= 2
                     for k, v in attempts.items()), "C2_RESTART_BUDGET")
        self.attempts = dict(attempts)
        changed = None
        if ready:
            key = cursor(body)
            need(key not in self.retired, "C2_RETIRED_CURSOR")
            if self.current_cursor is not None and self.current_cursor != key:
                old = self.current_cursor
                old_run, old_gen, old_session = old.split(":")
                new_run, new_gen, new_session = key.split(":")
                need(old_run == new_run and int(new_gen) > int(old_gen)
                     and new_session != old_session, "C2_CURSOR_REUSE")
                self.retired.add(old)
                changed = old
            self.current_cursor = key
        elif self.current_cursor is not None:
            # Once unavailable, an old public cursor can never return.
            self.retired.add(self.current_cursor)
        if self.started is None:
            need(elapsed <= MAX_STARTUP_SECONDS, "C2_STARTUP_TIMEOUT")
            if both:
                self.started = elapsed
                self.both_samples += 1
                self.previous_health = (True, True)
            return changed
        if not ready or not self.previous_health[0]:
            self.outage_seconds += dt
        if not both or not self.previous_health[1] or changed is not None:
            self.degraded_seconds += dt
        if not ready:
            if self.outage_since is None:
                self.outage_since = elapsed - dt
        elif self.outage_since is not None:
            need(elapsed - self.outage_since <= MAX_OUTAGE_SECONDS,
                 "C2_OUTAGE_TIMEOUT")
            self.outage_since = None
        if self.outage_since is not None:
            need(elapsed - self.outage_since <= MAX_OUTAGE_SECONDS,
                 "C2_OUTAGE_TIMEOUT")
        if not both or changed is not None:
            if self.degraded_since is None:
                self.degraded_since = elapsed - dt
                need(len(self.incidents) < MAX_INCIDENTS, "C2_INCIDENT_BUDGET")
        if self.degraded_since is not None:
            recovery = elapsed - self.degraded_since
            need(recovery <= MAX_RECOVERY_SECONDS, "C2_RECOVERY_TIMEOUT")
            if both:
                self.incidents.append({
                    "start_seconds": round(self.degraded_since, 6),
                    "recovery_seconds": round(recovery, 6),
                })
                self.degraded_since = None
        if both:
            self.both_samples += 1
        self.previous_health = (ready, both)
        need(self.outage_seconds <= MAX_TOTAL_OUTAGE_SECONDS,
             "C2_CUMULATIVE_OUTAGE")
        need(self.degraded_seconds <= MAX_TOTAL_DEGRADED_SECONDS,
             "C2_CUMULATIVE_DEGRADATION")
        return changed

    def finish(self):
        need(self.started is not None and self.last >= self.duration
             and self.previous_health == (True, True)
             and self.outage_since is None and self.degraded_since is None
             and self.both_samples >= int(self.duration * 2)
             and self.outage_seconds <= (self.last - self.started) * .005
             and self.degraded_seconds <= (self.last - self.started) * .01,
             "C2_FINAL_AVAILABILITY")
        return {
            "duration_seconds": self.duration,
            "observed_seconds": round(self.last, 6),
            "startup_seconds": round(self.started, 6),
            "samples": self.samples,
            "both_ready_samples": self.both_samples,
            "max_poll_gap_seconds": round(self.max_poll, 6),
            "outage_upper_bound_seconds": round(self.outage_seconds, 6),
            "degraded_upper_bound_seconds": round(self.degraded_seconds, 6),
            "recovered_incidents": self.incidents,
            "cursor_transitions": len(self.retired),
            "source_attempts": self.attempts,
            "zero_gap_certified": False,
            "historical_gap_recovered": False,
            "real_orders_enabled": False,
        }
