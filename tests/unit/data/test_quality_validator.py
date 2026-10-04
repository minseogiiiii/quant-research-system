from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.data import (
    DataQualityPolicy,
    DataQualityValidator,
    QualityIssueCode,
    QualityStatus,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage, Quote

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def quote(
    *,
    timestamp: datetime = NOW,
    source: str = "toss",
) -> Quote:
    return Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=timestamp,
        currency="KRW",
        source=source,
    )


def candle(
    minute: int,
    *,
    price: Decimal = Decimal("100"),
    volume: int = 1_000,
    timestamp: datetime | None = None,
) -> Candle:
    resolved_timestamp = timestamp or (NOW - timedelta(minutes=10 - minute))
    return Candle(
        symbol="005930",
        interval=CandleInterval.MINUTE_1,
        timestamp=resolved_timestamp,
        open_price=price,
        high_price=price,
        low_price=price,
        close_price=price,
        volume=volume,
        currency="KRW",
        source="toss",
    )


def test_fresh_quote_passes() -> None:
    result = DataQualityValidator().assess_quotes(
        (quote(),),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.PASS
    assert result.issues == ()


def test_stale_quote_warns_without_default_quarantine() -> None:
    result = DataQualityValidator().assess_quotes(
        (quote(timestamp=NOW - timedelta(hours=2)),),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.WARNING
    assert [issue.code for issue in result.issues] == [
        QualityIssueCode.STALE_QUOTE
    ]


def test_policy_can_quarantine_very_stale_quotes() -> None:
    validator = DataQualityValidator(
        DataQualityPolicy(
            stale_quote_warning_after=timedelta(minutes=10),
            stale_quote_quarantine_after=timedelta(hours=1),
        )
    )

    result = validator.assess_quotes(
        (quote(timestamp=NOW - timedelta(hours=2)),),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.QUARANTINE


def test_future_timestamp_is_quarantined() -> None:
    result = DataQualityValidator().assess_quotes(
        (quote(timestamp=NOW + timedelta(minutes=6)),),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.QUARANTINE
    assert result.issues[0].code is QualityIssueCode.FUTURE_TIMESTAMP


def test_source_mismatch_is_quarantined() -> None:
    result = DataQualityValidator().assess_quotes(
        (quote(source="other"),),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.QUARANTINE
    assert result.issues[0].code is QualityIssueCode.SOURCE_MISMATCH


def test_empty_quote_batch_warns() -> None:
    result = DataQualityValidator().assess_quotes(
        (),
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.WARNING
    assert result.issues[0].code is QualityIssueCode.EMPTY_DATASET


def test_extreme_price_move_is_warning_not_quarantine() -> None:
    page = CandlePage(
        candles=(
            candle(0, price=Decimal("100")),
            candle(1, price=Decimal("175")),
        ),
        next_before=None,
    )

    result = DataQualityValidator().assess_candle_page(
        page,
        now_utc=NOW,
        expected_source="toss",
    )

    assert result.status is QualityStatus.WARNING
    assert QualityIssueCode.EXTREME_PRICE_MOVE in {
        issue.code for issue in result.issues
    }


def test_intraday_gap_warns_but_overnight_gap_is_treated_as_session_break() -> None:
    validator = DataQualityValidator()
    gap_page = CandlePage(
        candles=(
            candle(0, timestamp=NOW - timedelta(minutes=20)),
            candle(1, timestamp=NOW - timedelta(minutes=10)),
        ),
        next_before=None,
    )
    overnight_page = CandlePage(
        candles=(
            candle(0, timestamp=NOW - timedelta(hours=20)),
            candle(1, timestamp=NOW - timedelta(minutes=1)),
        ),
        next_before=None,
    )

    gap_result = validator.assess_candle_page(
        gap_page,
        now_utc=NOW,
        expected_source="toss",
    )
    overnight_result = validator.assess_candle_page(
        overnight_page,
        now_utc=NOW,
        expected_source="toss",
    )

    assert QualityIssueCode.CANDLE_GAP in {
        issue.code for issue in gap_result.issues
    }
    assert QualityIssueCode.CANDLE_GAP not in {
        issue.code for issue in overnight_result.issues
    }


def test_zero_volume_and_flatline_runs_warn() -> None:
    candles = tuple(
        candle(index, volume=0)
        for index in range(5)
    )
    result = DataQualityValidator().assess_candle_page(
        CandlePage(candles=candles, next_before=None),
        now_utc=NOW,
        expected_source="toss",
    )

    codes = {issue.code for issue in result.issues}
    assert result.status is QualityStatus.WARNING
    assert QualityIssueCode.ZERO_VOLUME_RUN in codes
    assert QualityIssueCode.FLATLINE_RUN in codes
