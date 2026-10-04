import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from world_quant_system.data import (
    DataQualityReport,
    MarketDataNormalizer,
    NormalizedMarketDataConfigurationError,
    QualityDatasetKind,
    QualityStatus,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage


class FixedClock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 21, tzinfo=UTC)


class NaiveClock:
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 21)


def report() -> DataQualityReport:
    return DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(b"assessment").hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(b"raw").hexdigest(),
        dataset_kind=QualityDatasetKind.CANDLES,
        status=QualityStatus.PASS,
        checked_at=datetime(2026, 7, 21, tzinfo=UTC),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=1,
        issues=(),
    )


def page() -> CandlePage:
    price = Decimal("95000")
    return CandlePage(
        (
            Candle(
                symbol="005930",
                interval=CandleInterval.DAY_1,
                timestamp=datetime(2026, 7, 20, tzinfo=UTC),
                open_price=price,
                high_price=price + Decimal("100"),
                low_price=price - Decimal("100"),
                close_price=price,
                volume=1_000,
                currency="KRW",
                source="toss",
            ),
        ),
        None,
    )


@pytest.mark.asyncio
async def test_normalizer_persists_quality_approved_page(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    normalizer = MarketDataNormalizer(store, clock=FixedClock())

    records = await normalizer.normalize_candle_page(report(), page())

    assert len(records) == 1
    assert records[0].normalizer_version == "1.0.0"


@pytest.mark.asyncio
async def test_normalizer_rejects_naive_clock(tmp_path: Path) -> None:
    normalizer = MarketDataNormalizer(
        SQLiteNormalizedMarketDataStore(tmp_path / "normalized"),
        clock=NaiveClock(),
    )

    with pytest.raises(NormalizedMarketDataConfigurationError):
        await normalizer.normalize_candle_page(report(), page())
