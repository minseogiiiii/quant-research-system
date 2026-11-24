"""Read-only dual-source selection policy, independent of collector transport.

No source is promoted without independent readiness, epoch and freshness
evidence. Switching is a new public generation; old cursors cannot carry over.
This module does not merge books or certify missing source intervals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str
    session: str
    observed_ns: int
    receiver_age_ms: int
    both_books_valid: bool
    snapshot_session_bound: bool
    independent_book_check: bool
    ready: bool
    provenance: str
    orders_enabled: bool = False

    def admissible(self, now_ns: int, max_age_ns: int) -> bool:
        return (
            self.name in ("primary", "secondary")
            and type(self.session) is str
            and re.fullmatch("[0-9a-f]{32}", self.session) is not None
            and type(self.provenance) is str
            and re.fullmatch("[0-9a-f]{64}", self.provenance) is not None
            and type(self.observed_ns) is int
            and 0 <= now_ns - self.observed_ns <= max_age_ns
            and type(self.receiver_age_ms) is int
            and 0 <= self.receiver_age_ms * 1_000_000
            + now_ns - self.observed_ns <= max_age_ns
            and self.both_books_valid is True
            and self.snapshot_session_bound is True
            and self.independent_book_check is True
            and self.ready is True
            and self.orders_enabled is False
        )


class Arbiter:
    """Sticky source, immediate revocation, conservative independent failover."""

    def __init__(self, max_age_ns=3_000_000_000, confirmations=3):
        if (type(max_age_ns) is not int or max_age_ns <= 0
                or type(confirmations) is not int or confirmations < 2):
            raise ValueError("FAILOVER_INVALID_POLICY")
        self.max_age_ns = max_age_ns
        self.confirmations = confirmations
        self.active = None
        self.generation = 0
        self.last_ns = -1
        self.last_seen = {}
        self.candidates = {}
        self.retired = set()

    def update(self, now_ns, primary=None, secondary=None, *, disagreement=False):
        if type(now_ns) is not int or now_ns <= self.last_ns:
            raise ValueError("FAILOVER_CLOCK_REGRESSION")
        if type(disagreement) is not bool:
            raise ValueError("FAILOVER_INVALID_DISAGREEMENT")
        raw = {"primary": primary, "secondary": secondary}
        if any(x is not None and not isinstance(x, Source) for x in raw.values()):
            raise ValueError("FAILOVER_INVALID_SOURCE")
        self.last_ns = now_ns
        # Source observations must not be aliases of the same collector.
        if primary is not None and secondary is not None:
            if (primary.session == secondary.session
                    or primary.provenance == secondary.provenance):
                disagreement = True
        valid = {}
        for name, item in raw.items():
            if (disagreement or item is None or item.name != name
                    or not item.admissible(now_ns, self.max_age_ns)
                    or (name, item.session) in self.retired):
                self.candidates.pop(name, None)
                continue
            key = (name, item.session, item.provenance)
            prior_ns = self.last_seen.get(name, -1)
            if item.observed_ns < prior_ns:
                self.candidates.pop(name, None)
                continue
            old_key, count = self.candidates.get(name, (None, 0))
            if item.observed_ns > prior_ns:
                count = count + 1 if key == old_key else 1
                self.last_seen[name] = item.observed_ns
                self.candidates[name] = (key, count)
            elif key != old_key:
                self.candidates.pop(name, None)
                continue
            valid[name] = key
        revoked = False
        if self.active is not None and valid.get(self.active[0]) != self.active:
            name, session, _provenance = self.active
            # A session lost from the active route must never be reused as
            # an active session, even if its cached observations return.
            self.retired.add((name, session))
            valid = {k: v for k, v in valid.items()
                     if (k, v[1]) not in self.retired}
            self.candidates.pop(name, None)
            self.active = None
            self.generation += 1
            revoked = True
        if self.active is None and not disagreement:
            for name in ("primary", "secondary"):
                if (name in valid and self.candidates.get(name, (None, 0))[1]
                        >= self.confirmations):
                    self.active = valid[name]
                    self.generation += 1
                    break
        return {
            "status": "HEALTHY" if self.active is not None else "QUARANTINED",
            "active_source": self.active[0] if self.active else None,
            "source_session": self.active[1] if self.active else None,
            "generation": self.generation,
            "revoked": revoked,
            "historical_gap_recovered": False,
            "zero_gap_certified": False,
            "real_orders_enabled": False,
        }

    def cursor_valid(self, generation, session):
        return (self.active is not None and type(generation) is int
                and generation == self.generation and session == self.active[1])
