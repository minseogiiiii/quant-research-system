from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

from world_quant_system.data.normalized_models import (
    NormalizedCandleCursor,
    NormalizedMarketDataReader,
    canonical_json_bytes,
)
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.replay.models import (
    ReplayConfig,
    ReplayConfigurationError,
    ReplayEvent,
    ReplayInvariantError,
    ReplayRunResult,
    replay_event_document,
)


class ReplayEventHandler(Protocol):
    async def on_candle(self, event: ReplayEvent, clock: ReplayClock) -> None:
        """Consume one chronological candle without access to future events."""
        ...


class ReplayClock:
    """Monotonic clock advanced only by the replay engine."""

    def __init__(self) -> None:
        self._current: datetime | None = None

    @property
    def current(self) -> datetime | None:
        return self._current

    def advance_to(self, value: datetime) -> None:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ReplayConfigurationError(
                "Replay clock requires timezone-aware timestamps."
            )
        normalized = value.astimezone(UTC)
        if self._current is not None and normalized < self._current:
            raise ReplayInvariantError("Replay clock cannot move backwards.")
        self._current = normalized


class NoOpReplayHandler:
    async def on_candle(self, event: ReplayEvent, clock: ReplayClock) -> None:
        del event, clock


class DeterministicReplayEngine:
    """Page through normalized candles with stable order and no look-ahead."""

    def __init__(
        self,
        reader: NormalizedMarketDataReader,
        config: ReplayConfig,
        *,
        clock: ReplayClock | None = None,
    ) -> None:
        self._reader = reader
        self._config = config
        self._clock = clock or ReplayClock()
        self._has_run = False

    @property
    def clock(self) -> ReplayClock:
        return self._clock

    async def run(
        self,
        handler: ReplayEventHandler | None = None,
    ) -> ReplayRunResult:
        if self._has_run:
            raise ReplayConfigurationError(
                "Replay engine instances are single-use to prevent state leakage."
            )
        self._has_run = True
        consumer = handler or NoOpReplayHandler()
        digest = hashlib.sha256()
        cursor: NormalizedCandleCursor | None = None
        sequence = 0
        pass_count = 0
        warning_count = 0
        first_event_at: datetime | None = None
        last_event_at: datetime | None = None
        seen_item_ids: set[str] = set()
        previous_order_key: tuple[datetime, str, str] | None = None

        while True:
            page = await self._reader.query_candles(
                symbols=self._config.symbols,
                interval=self._config.interval,
                start=self._config.start,
                end=self._config.end,
                statuses=self._config.statuses,
                after=cursor,
                limit=self._config.page_size,
            )
            if not page:
                break

            for record in page:
                candle = record.candle
                event_time = candle.timestamp.astimezone(UTC)
                order_key = (event_time, candle.symbol, record.item_id)
                if previous_order_key is not None and order_key <= previous_order_key:
                    raise ReplayInvariantError(
                        "Normalized reader returned non-monotonic replay data."
                    )
                if record.item_id in seen_item_ids:
                    raise ReplayInvariantError(
                        "Normalized reader returned a duplicate replay item."
                    )
                seen_item_ids.add(record.item_id)
                previous_order_key = order_key
                self._clock.advance_to(event_time)

                event = ReplayEvent(
                    sequence=sequence,
                    event_time=event_time,
                    record=record,
                )
                await consumer.on_candle(event, self._clock)
                digest.update(canonical_json_bytes(replay_event_document(event)))

                if first_event_at is None:
                    first_event_at = event_time
                last_event_at = event_time
                if record.quality_status is QualityStatus.PASS:
                    pass_count += 1
                elif record.quality_status is QualityStatus.WARNING:
                    warning_count += 1
                else:
                    raise ReplayInvariantError(
                        "Replay encountered a non-consumable quality status."
                    )
                sequence += 1

            last = page[-1]
            cursor = NormalizedCandleCursor(
                timestamp=last.candle.timestamp,
                symbol=last.candle.symbol,
                item_id=last.item_id,
            )
            if len(page) < self._config.page_size:
                break

        return ReplayRunResult(
            config_fingerprint=self._config.fingerprint,
            event_digest=digest.hexdigest(),
            event_count=sequence,
            pass_count=pass_count,
            warning_count=warning_count,
            first_event_at=first_event_at,
            last_event_at=last_event_at,
        )
