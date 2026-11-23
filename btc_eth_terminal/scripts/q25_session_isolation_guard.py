"""Q25 P0-B: read-only, fail-closed source-epoch market state isolation.

The Q14 immutable v36 engine already gates public APIs by source status.
This opt-in CI/observer subclass adds immediate in-memory quote/candle/trade
erasure on ANY invalidation (gap, stale source, rejected frame, stop) while
leaving the pinned original engine and source recorder unchanged.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from v23_causal_microstructure import Book
from v35_replay_service import PRODUCTS
from v36_live_engine import MAX_RETAINED, LiveMarketEngine

INVALID_STATES = frozenset(("QUARANTINED", "RECONNECTING", "STALE", "STOPPED"))


class SourceEpochIsolatedEngine(LiveMarketEngine):
    """A defensive erase-on-invalid observer, not a trading admission path."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.q25_revocations_total = 0
        self.q25_last_revocation_class = None

    def _erase_market_state(self) -> None:
        # Called with the original engine's RLock. This destroys retained
        # market data and old references, not the source diagnostic counters.
        self.books = {p: Book() for p in PRODUCTS}
        self.ofi = {p: Decimal(0) for p in PRODUCTS}
        self.last_l2 = {p: None for p in PRODUCTS}
        self.last_l2_ingest_mono = {p: None for p in PRODUCTS}
        self.last_pub = {p: None for p in PRODUCTS}
        self.recent_trades = {p: deque(maxlen=24) for p in PRODUCTS}
        self.candle_open = {p: None for p in PRODUCTS}
        self.candles = {p: deque(maxlen=120) for p in PRODUCTS}
        self.rows = {p: deque(maxlen=MAX_RETAINED) for p in PRODUCTS}

    def _invalid(self, reason: str, phase: str = "QUARANTINED") -> None:
        with self._cv:
            # The inherited event can wake SSE waiters, but Condition waiters
            # cannot observe state until this lock has been released.
            super()._invalid(reason, phase)
            if phase in INVALID_STATES:
                self._erase_market_state()
                self.q25_revocations_total += 1
                # The reason may contain untrusted source bytes. Never copy
                # it directly into public counters or diagnostics.
                prefix = reason.split(":", 1)[0]
                known = {
                    "SOURCE_ENVELOPE_REJECTED",
                    "SOURCE_REQUIRES_NEW_CONNECTION",
                    "SOURCE_STOPPED",
                    "SOURCE_TRANSPORT_LOST",
                    "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP",
                    "COLLECTOR_OBSERVATION_SOCKET_CLOSED",
                    "NO_COLLECTOR_MESSAGES_WITHIN_STALENESS_BUDGET",
                    "HEARTBEAT_STALE",
                    "PRODUCT_L2_STALE_OR_CLOCK_ANOMALY",
                    "LIVE_ADMISSION_REJECTED",
                    "SYNTHETIC_STOP",
                }
                self.q25_last_revocation_class = (
                    prefix if prefix in known else "OTHER_REDACTED"
                )
                self._cv.notify_all()

    def health(self) -> dict:
        with self._cv:
            health = super().health()
            # Market prices/quotes are NEVER added to private health.
            health.update({
                "q25_isolation_schema": "q25_source_epoch_isolation_v1",
                "q25_revocations_total": self.q25_revocations_total,
                "q25_last_revocation_class": self.q25_last_revocation_class,
                "q25_retained_rows_per_product": {
                    p: len(self.rows[p]) for p in PRODUCTS
                },
                "q25_open_candles_per_product": {
                    p: int(self.candle_open[p] is not None) for p in PRODUCTS
                },
                "q25_quote_buffers_scrubbed": all(
                    not self.rows[p]
                    and not self.recent_trades[p]
                    and not self.candles[p]
                    and self.candle_open[p] is None
                    and self.last_l2[p] is None
                    and self.last_pub[p] is None
                    and self.ofi[p] == 0
                    and not self.books[p].level["bid"]
                    and not self.books[p].level["offer"]
                    and not self.books[p].initialized
                    for p in PRODUCTS
                ),
                "q25_orders_enabled": False,
            })
            return health
