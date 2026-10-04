from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.adapters.toss import (
    TossConfigurationError,
    TossHttpClient,
    TossInvalidResponseError,
    TossMarketDataProvider,
    TossRequest,
    TossResponse,
)
from world_quant_system.data import (
    DataQualityPolicy,
    DataQualityRejectedError,
    DataQualityValidator,
    FileRawMarketDataStore,
    MarketDataNormalizer,
    MarketDataQualityGate,
    QualityDatasetKind,
    QualityStatus,
    SQLiteDataQualityStore,
    SQLiteNormalizedMarketDataStore,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class FixedClock:
    def now_utc(self) -> datetime:
        return NOW


class CapturingTransport:
    def __init__(self, responses: Sequence[TossResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[TossRequest] = []

    async def send(self, request: TossRequest) -> TossResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No fake response remains.")
        return self.responses.pop(0)


def response(*, timestamp: str, price: object = "95000") -> TossResponse:
    return TossResponse(
        status_code=200,
        headers={"X-Request-Id": f"quality-{timestamp}"},
        json_body={
            "result": [
                {
                    "symbol": "005930",
                    "timestamp": timestamp,
                    "lastPrice": price,
                    "currency": "KRW",
                }
            ]
        },
    )


def build_provider(
    tmp_path: Path,
    responses: Sequence[TossResponse],
    *,
    validator: DataQualityValidator | None = None,
) -> tuple[TossMarketDataProvider, SQLiteDataQualityStore]:
    raw_store = FileRawMarketDataStore(tmp_path / "raw")
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        validator=validator,
        clock=FixedClock(),
    )
    client = TossHttpClient(
        "https://example.test",
        transport=CapturingTransport(responses),
    )
    return (
        TossMarketDataProvider(
            client,
            clock=FixedClock(),
            raw_recorder=raw_store,
            quality_gate=gate,
        ),
        quality_store,
    )


def test_quality_gate_requires_raw_recorder(tmp_path: Path) -> None:
    raw_store = FileRawMarketDataStore(tmp_path / "raw")
    gate = MarketDataQualityGate(
        raw_store,
        SQLiteDataQualityStore(tmp_path / "quality"),
    )
    client = TossHttpClient("https://example.test")

    with pytest.raises(TossConfigurationError, match="requires a raw"):
        TossMarketDataProvider(client, quality_gate=gate)


@pytest.mark.asyncio
async def test_warning_report_allows_quote_to_continue(tmp_path: Path) -> None:
    provider, store = build_provider(
        tmp_path,
        [response(timestamp="2026-07-21T10:00:00+00:00")],
    )

    quote = await provider.get_quote("005930")
    reports = await store.query()

    assert quote.symbol == "005930"
    assert len(reports) == 1
    assert reports[0].status is QualityStatus.WARNING


@pytest.mark.asyncio
async def test_quarantine_report_blocks_quote(tmp_path: Path) -> None:
    validator = DataQualityValidator(
        DataQualityPolicy(
            stale_quote_warning_after=timedelta(minutes=5),
            stale_quote_quarantine_after=timedelta(hours=1),
        )
    )
    provider, store = build_provider(
        tmp_path,
        [response(timestamp="2026-07-21T10:00:00+00:00")],
        validator=validator,
    )

    with pytest.raises(DataQualityRejectedError):
        await provider.get_quote("005930")

    reports = await store.quarantined()
    assert len(reports) == 1
    assert reports[0].dataset_kind is QualityDatasetKind.QUOTES


@pytest.mark.asyncio
async def test_parse_failure_is_persisted_as_quarantine(tmp_path: Path) -> None:
    provider, store = build_provider(
        tmp_path,
        [response(timestamp="2026-07-21T12:00:00+00:00", price="NaN")],
    )

    with pytest.raises(TossInvalidResponseError):
        await provider.get_quote("005930")

    reports = await store.quarantined()
    assert len(reports) == 1
    assert reports[0].dataset_kind is QualityDatasetKind.PARSE_FAILURE


def test_normalizer_requires_quality_gate(tmp_path: Path) -> None:
    client = TossHttpClient("https://example.test")
    normalizer = MarketDataNormalizer(
        SQLiteNormalizedMarketDataStore(tmp_path / "normalized"),
        clock=FixedClock(),
    )

    with pytest.raises(TossConfigurationError, match="quality gate"):
        TossMarketDataProvider(client, normalizer=normalizer)


@pytest.mark.asyncio
async def test_quality_approved_quote_is_normalized_before_return(
    tmp_path: Path,
) -> None:
    raw_store = FileRawMarketDataStore(tmp_path / "raw")
    quality_store = SQLiteDataQualityStore(tmp_path / "quality")
    normalized_store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    gate = MarketDataQualityGate(
        raw_store,
        quality_store,
        clock=FixedClock(),
    )
    normalizer = MarketDataNormalizer(normalized_store, clock=FixedClock())
    provider = TossMarketDataProvider(
        TossHttpClient(
            "https://example.test",
            transport=CapturingTransport(
                [response(timestamp="2026-07-21T12:00:00+00:00")]
            ),
        ),
        clock=FixedClock(),
        raw_recorder=raw_store,
        quality_gate=gate,
        normalizer=normalizer,
    )

    quote = await provider.get_quote("005930")
    stored = await normalized_store.latest_quote("005930")

    assert stored.quote == quote
    assert stored.quality_status is QualityStatus.PASS
