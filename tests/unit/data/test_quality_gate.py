from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.data import (
    DataQualityPolicy,
    DataQualityValidator,
    FileRawMarketDataStore,
    MarketDataQualityGate,
    QualityIssueCode,
    QualityStatus,
    RawMarketDataCapture,
    RawMarketDataMetadata,
    SQLiteDataQualityStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage, Quote

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class FixedClock:
    def now_utc(self) -> datetime:
        return NOW


async def raw_metadata(
    tmp_path: Path,
    *,
    endpoint: str = "/api/v1/prices",
) -> tuple[FileRawMarketDataStore, RawMarketDataMetadata]:
    raw_store = FileRawMarketDataStore(tmp_path / "raw")
    metadata = await raw_store.record(
        RawMarketDataCapture(
            provider="toss",
            endpoint=endpoint,
            request_params={"symbols": "005930"},
            captured_at=NOW,
            status_code=200,
            response_headers={"X-Request-Id": "quality-test"},
            json_body={"result": []},
            request_id="quality-test",
            idempotency_key=f"quality-test:{endpoint}",
        )
    )
    return raw_store, metadata


@pytest.mark.asyncio
async def test_gate_persists_pass_report(tmp_path: Path) -> None:
    raw_store, metadata = await raw_metadata(tmp_path)
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        clock=FixedClock(),
    )
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW,
        currency="KRW",
        source="toss",
    )

    report = await gate.assess_quotes(metadata, (quote,))

    assert report.status is QualityStatus.PASS
    assert await quality_store.get(report.report_id) == report


@pytest.mark.asyncio
async def test_gate_quarantines_tampered_raw_record(tmp_path: Path) -> None:
    raw_store, metadata = await raw_metadata(tmp_path)
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        clock=FixedClock(),
    )
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW,
        currency="KRW",
        source="toss",
    )
    archive = raw_store.root / metadata.relative_path
    archive.write_bytes(b"tampered")

    report = await gate.assess_quotes(metadata, (quote,))

    assert report.status is QualityStatus.QUARANTINE
    assert QualityIssueCode.RAW_INTEGRITY_FAILURE in {
        issue.code for issue in report.issues
    }


@pytest.mark.asyncio
async def test_gate_preserves_extreme_move_as_warning(tmp_path: Path) -> None:
    raw_store, metadata = await raw_metadata(
        tmp_path,
        endpoint="/api/v1/candles",
    )
    gate = MarketDataQualityGate(
        raw_store,
        SQLiteDataQualityStore(tmp_path / "quality"),
        clock=FixedClock(),
    )
    first = Candle(
        symbol="005930",
        interval=CandleInterval.MINUTE_1,
        timestamp=NOW - timedelta(minutes=2),
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("100"),
        volume=100,
        currency="KRW",
        source="toss",
    )
    second = Candle(
        symbol="005930",
        interval=CandleInterval.MINUTE_1,
        timestamp=NOW - timedelta(minutes=1),
        open_price=Decimal("175"),
        high_price=Decimal("175"),
        low_price=Decimal("175"),
        close_price=Decimal("175"),
        volume=100,
        currency="KRW",
        source="toss",
    )

    report = await gate.assess_candle_page(
        metadata,
        CandlePage(candles=(first, second), next_before=None),
    )

    assert report.status is QualityStatus.WARNING


@pytest.mark.asyncio
async def test_gate_can_quarantine_stale_quote_by_policy(tmp_path: Path) -> None:
    raw_store, metadata = await raw_metadata(tmp_path)
    validator = DataQualityValidator(
        DataQualityPolicy(
            stale_quote_warning_after=timedelta(minutes=5),
            stale_quote_quarantine_after=timedelta(hours=1),
        )
    )
    gate = MarketDataQualityGate(
        raw_store,
        SQLiteDataQualityStore(tmp_path / "quality"),
        validator=validator,
        clock=FixedClock(),
    )
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW - timedelta(hours=2),
        currency="KRW",
        source="toss",
    )

    report = await gate.assess_quotes(metadata, (quote,))

    assert report.status is QualityStatus.QUARANTINE


@pytest.mark.asyncio
async def test_reassessment_is_deterministic_when_wall_clock_changes(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now_utc(self) -> datetime:
            return self.current

    raw_store, metadata = await raw_metadata(tmp_path)
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    clock = MutableClock()
    gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        clock=clock,
    )
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW - timedelta(minutes=20),
        currency="KRW",
        source="toss",
    )

    first = await gate.assess_quotes(metadata, (quote,))
    clock.current = NOW + timedelta(days=30)
    second = await gate.assess_quotes(metadata, (quote,))

    assert first.status is QualityStatus.WARNING
    assert second == first
    assert await quality_store.query() == (first,)


@pytest.mark.asyncio
async def test_policy_changes_produce_distinct_assessment_keys(
    tmp_path: Path,
) -> None:
    raw_store, metadata = await raw_metadata(tmp_path)
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW - timedelta(minutes=20),
        currency="KRW",
        source="toss",
    )
    warning_gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        validator=DataQualityValidator(
            DataQualityPolicy(stale_quote_warning_after=timedelta(minutes=15))
        ),
        clock=FixedClock(),
    )
    pass_gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        validator=DataQualityValidator(
            DataQualityPolicy(stale_quote_warning_after=timedelta(minutes=30))
        ),
        clock=FixedClock(),
    )

    warning_report = await warning_gate.assess_quotes(metadata, (quote,))
    pass_report = await pass_gate.assess_quotes(metadata, (quote,))

    assert warning_report.status is QualityStatus.WARNING
    assert pass_report.status is QualityStatus.PASS
    assert warning_report.assessment_key != pass_report.assessment_key
    assert warning_report.policy_fingerprint != pass_report.policy_fingerprint
