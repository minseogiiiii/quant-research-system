from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from world_quant_system.data.normalized_models import (
    NormalizedCandleRecord,
    NormalizedMarketDataConfigurationError,
    NormalizedMarketDataWriter,
    NormalizedQuoteRecord,
)
from world_quant_system.data.quality_models import DataQualityReport
from world_quant_system.domain.models import CandlePage, Quote


class NormalizationClock(Protocol):
    def now_utc(self) -> datetime:
        """Return a timezone-aware current time."""
        ...


class SystemNormalizationClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class MarketDataNormalizer:
    """Convert quality-approved domain data into versioned append-only records."""

    VERSION = "1.0.0"

    def __init__(
        self,
        writer: NormalizedMarketDataWriter,
        *,
        clock: NormalizationClock | None = None,
    ) -> None:
        self._writer = writer
        self._clock = clock or SystemNormalizationClock()

    async def normalize_quotes(
        self,
        report: DataQualityReport,
        quotes: Sequence[Quote],
    ) -> tuple[NormalizedQuoteRecord, ...]:
        return await self._writer.save_quotes(
            quotes,
            report,
            normalized_at=self._now_utc(),
            normalizer_version=self.VERSION,
        )

    async def normalize_candle_page(
        self,
        report: DataQualityReport,
        page: CandlePage,
    ) -> tuple[NormalizedCandleRecord, ...]:
        return await self._writer.save_candle_page(
            page,
            report,
            normalized_at=self._now_utc(),
            normalizer_version=self.VERSION,
        )

    def _now_utc(self) -> datetime:
        now = self._clock.now_utc()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise NormalizedMarketDataConfigurationError(
                "Normalization clock must return a timezone-aware datetime."
            )
        return now.astimezone(UTC)
