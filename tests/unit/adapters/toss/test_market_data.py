from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import pytest

from world_quant_system.adapters.toss import (
    TossAuthenticationError,
    TossConfigurationError,
    TossHttpClient,
    TossInvalidResponseError,
    TossMarketDataProvider,
    TossRateLimitError,
    TossRequest,
    TossResponse,
    TossServerResponseError,
    TossTokenManager,
)
from world_quant_system.adapters.toss.token import TokenIssueResponse
from world_quant_system.domain.models import CandleInterval

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now


class CapturingTransport:
    def __init__(self, responses: Sequence[TossResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[TossRequest] = []

    async def send(self, request: TossRequest) -> TossResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("No fake response remains.")

        return self._responses.pop(0)


class SequenceIssuer:
    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = list(tokens)
        self.call_count = 0

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1
        if not self._tokens:
            raise AssertionError("No fake token remains.")

        return TokenIssueResponse(
            access_token=self._tokens.pop(0),
            token_type="Bearer",
            expires_in=86_400,
        )


def price_item(
    symbol: str = "005930",
    *,
    timestamp: str = "2026-07-21T20:59:59+09:00",
    price: object = "95000",
    currency: object = "KRW",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "lastPrice": price,
        "currency": currency,
    }


def candle_item(
    timestamp: str,
    *,
    open_price: object = "71600",
    high_price: object = "72300",
    low_price: object = "71500",
    close_price: object = "72000",
    volume: object = "3521000",
    currency: object = "KRW",
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "openPrice": open_price,
        "highPrice": high_price,
        "lowPrice": low_price,
        "closePrice": close_price,
        "volume": volume,
        "currency": currency,
    }


def response_with_result(result: object) -> TossResponse:
    return TossResponse(
        status_code=200,
        json_body={"result": result},
    )


def provider_with_responses(
    responses: Sequence[TossResponse],
    *,
    clock: FixedClock | None = None,
) -> tuple[TossMarketDataProvider, CapturingTransport]:
    transport = CapturingTransport(responses)
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
    )
    provider = TossMarketDataProvider(
        client,
        clock=clock or FixedClock(),
    )
    return provider, transport


@pytest.mark.asyncio
async def test_get_quote_builds_official_prices_request() -> None:
    provider, transport = provider_with_responses(
        [response_with_result([price_item()])]
    )

    quote = await provider.get_quote(" 005930 ")

    assert quote.symbol == "005930"
    assert str(quote.price) == "95000"
    assert quote.currency == "KRW"
    assert quote.source == "toss"
    assert transport.requests[0].url == "https://example.test/api/v1/prices"
    assert transport.requests[0].params == {"symbols": "005930"}


@pytest.mark.asyncio
async def test_get_quotes_preserves_order_and_deduplicates_wire_request() -> None:
    provider, transport = provider_with_responses(
        [
            response_with_result(
                [
                    price_item(
                        "005930",
                        price="95000",
                        currency="KRW",
                    ),
                    price_item(
                        "AAPL",
                        price="225.50",
                        currency="USD",
                    ),
                ]
            )
        ]
    )

    quotes = await provider.get_quotes(["aapl", "005930", "AAPL"])

    assert [quote.symbol for quote in quotes] == ["AAPL", "005930", "AAPL"]
    assert [quote.currency for quote in quotes] == ["USD", "KRW", "USD"]
    assert transport.requests[0].params == {"symbols": "AAPL,005930"}


@pytest.mark.asyncio
async def test_empty_quote_request_avoids_transport() -> None:
    provider, transport = provider_with_responses([])

    assert await provider.get_quotes([]) == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_more_than_200_unique_symbols_is_rejected() -> None:
    provider, transport = provider_with_responses([])
    symbols = [f"S{index}" for index in range(201)]

    with pytest.raises(TossConfigurationError, match="more than 200"):
        await provider.get_quotes(symbols)

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    ["", "   ", "AAPL,MSFT", "AAPL/MSFT", "AAPL?"],
)
async def test_invalid_symbol_is_rejected_before_transport(symbol: str) -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError):
        await provider.get_quote(symbol)

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lastPrice", "0"),
        ("lastPrice", "-1"),
        ("lastPrice", "NaN"),
        ("lastPrice", 95000),
        ("currency", "KR"),
        ("currency", 1),
        ("timestamp", "not-a-timestamp"),
        ("timestamp", "2026-07-21T11:59:59"),
        ("timestamp", "2026-07-21T12:06:00+00:00"),
    ],
)
async def test_malformed_price_fields_are_rejected(
    field: str,
    value: object,
) -> None:
    item = price_item()
    item[field] = value
    provider, _ = provider_with_responses([response_with_result([item])])

    with pytest.raises(TossInvalidResponseError):
        await provider.get_quote("005930")


@pytest.mark.asyncio
async def test_price_response_must_include_every_requested_symbol() -> None:
    provider, _ = provider_with_responses(
        [response_with_result([price_item("005930")])]
    )

    with pytest.raises(TossInvalidResponseError, match="every requested"):
        await provider.get_quotes(["005930", "000660"])


@pytest.mark.asyncio
async def test_price_response_rejects_duplicate_symbol() -> None:
    provider, _ = provider_with_responses(
        [response_with_result([price_item(), price_item()])]
    )

    with pytest.raises(TossInvalidResponseError, match="duplicate"):
        await provider.get_quote("005930")


@pytest.mark.asyncio
async def test_price_response_rejects_unexpected_symbol() -> None:
    provider, _ = provider_with_responses(
        [response_with_result([price_item("000660")])]
    )

    with pytest.raises(TossInvalidResponseError, match="unexpected"):
        await provider.get_quote("005930")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [None, {}, "not-a-list"],
)
async def test_price_response_requires_result_list(result: object) -> None:
    provider, _ = provider_with_responses([response_with_result(result)])

    with pytest.raises(TossInvalidResponseError):
        await provider.get_quote("005930")


@pytest.mark.asyncio
async def test_get_candles_validates_and_returns_oldest_first() -> None:
    newer = candle_item("2026-07-21T09:00:00+09:00")
    older = candle_item(
        "2026-07-20T09:00:00+09:00",
        close_price="71600",
    )
    provider, transport = provider_with_responses(
        [
            response_with_result(
                {
                    "candles": [newer, older],
                    "nextBefore": "2026-07-20T09:00:00+09:00",
                }
            )
        ]
    )

    page = await provider.get_candles(
        "005930",
        CandleInterval.DAY_1,
        count=2,
        adjusted=False,
    )

    assert [candle.timestamp.day for candle in page.candles] == [20, 21]
    assert page.next_before == datetime.fromisoformat("2026-07-20T09:00:00+09:00")
    assert transport.requests[0].url == "https://example.test/api/v1/candles"
    assert transport.requests[0].params == {
        "symbol": "005930",
        "interval": "1d",
        "count": "2",
        "adjusted": "false",
    }


@pytest.mark.asyncio
async def test_get_candles_encodes_timezone_aware_before_parameter() -> None:
    before = datetime.fromisoformat("2026-07-20T09:00:00+09:00")
    provider, transport = provider_with_responses(
        [response_with_result({"candles": [], "nextBefore": None})]
    )

    await provider.get_candles(
        "AAPL",
        CandleInterval.MINUTE_1,
        before=before,
    )

    assert transport.requests[0].params["before"] == before.isoformat()
    assert transport.requests[0].params["interval"] == "1m"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 201, True])
async def test_invalid_candle_count_is_rejected(count: int) -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError, match="between 1 and 200"):
        await provider.get_candles(
            "005930",
            CandleInterval.DAY_1,
            count=count,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_string_interval_is_rejected_at_runtime() -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError, match="CandleInterval"):
        await provider.get_candles(
            "005930",
            cast(CandleInterval, "1d"),
        )

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before",
    [
        datetime(2026, 7, 20, 9, 0),
        datetime(2026, 7, 21, 12, 6, tzinfo=UTC),
    ],
)
async def test_invalid_before_timestamp_is_rejected(before: datetime) -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError):
        await provider.get_candles(
            "005930",
            CandleInterval.DAY_1,
            before=before,
        )

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"highPrice": "70000", "lowPrice": "71000"},
        {"openPrice": "69000"},
        {"closePrice": "74000"},
        {"volume": "-1"},
        {"volume": "1.5"},
        {"openPrice": 71600},
        {"currency": "KR"},
        {"timestamp": "2026-07-21T12:06:00+00:00"},
    ],
)
async def test_invalid_candle_fields_are_rejected(
    changes: dict[str, object],
) -> None:
    item = candle_item("2026-07-21T09:00:00+09:00")
    item.update(changes)
    provider, _ = provider_with_responses(
        [response_with_result({"candles": [item], "nextBefore": None})]
    )

    with pytest.raises(TossInvalidResponseError):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
async def test_candle_response_rejects_duplicate_timestamps() -> None:
    item = candle_item("2026-07-21T09:00:00+09:00")
    provider, _ = provider_with_responses(
        [response_with_result({"candles": [item, dict(item)], "nextBefore": None})]
    )

    with pytest.raises(TossInvalidResponseError, match="duplicated"):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
async def test_candle_response_rejects_nonmonotonic_order() -> None:
    items = [
        candle_item("2026-07-19T09:00:00+09:00"),
        candle_item("2026-07-21T09:00:00+09:00"),
        candle_item("2026-07-20T09:00:00+09:00"),
    ]
    provider, _ = provider_with_responses(
        [response_with_result({"candles": items, "nextBefore": None})]
    )

    with pytest.raises(TossInvalidResponseError, match="non-monotonic"):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
async def test_candle_response_rejects_mixed_currency() -> None:
    items = [
        candle_item("2026-07-20T09:00:00+09:00", currency="KRW"),
        candle_item("2026-07-21T09:00:00+09:00", currency="USD"),
    ]
    provider, _ = provider_with_responses(
        [response_with_result({"candles": items, "nextBefore": None})]
    )

    with pytest.raises(TossInvalidResponseError, match="page invariant"):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
async def test_candle_response_rejects_more_items_than_requested() -> None:
    items = [
        candle_item("2026-07-20T09:00:00+09:00"),
        candle_item("2026-07-21T09:00:00+09:00"),
    ]
    provider, _ = provider_with_responses(
        [response_with_result({"candles": items, "nextBefore": None})]
    )

    with pytest.raises(TossInvalidResponseError, match="requested count"):
        await provider.get_candles(
            "005930",
            CandleInterval.DAY_1,
            count=1,
        )


@pytest.mark.asyncio
async def test_empty_candle_page_rejects_next_before() -> None:
    provider, _ = provider_with_responses(
        [
            response_with_result(
                {
                    "candles": [],
                    "nextBefore": "2026-07-20T09:00:00+09:00",
                }
            )
        ]
    )

    with pytest.raises(TossInvalidResponseError, match="Empty candle"):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
async def test_rate_limit_error_propagates_without_parsing() -> None:
    provider, transport = provider_with_responses(
        [TossResponse(status_code=429, headers={"Retry-After": "10"})]
    )

    with pytest.raises(TossRateLimitError) as captured:
        await provider.get_quote("005930")

    assert captured.value.retry_after_seconds == 10
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_server_error_propagates_without_parsing() -> None:
    provider, transport = provider_with_responses([TossResponse(status_code=500)])

    with pytest.raises(TossServerResponseError):
        await provider.get_candles("005930", CandleInterval.DAY_1)

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_provider_uses_authenticated_client_401_refresh_once() -> None:
    issuer = SequenceIssuer(["old-token", "replacement-token"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport(
        [
            TossResponse(status_code=401),
            response_with_result([price_item()]),
        ]
    )
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )
    provider = TossMarketDataProvider(client, clock=FixedClock())

    quote = await provider.get_quote("005930")

    assert quote.price == 95000
    assert issuer.call_count == 2
    assert len(transport.requests) == 2
    assert transport.requests[0].headers["Authorization"] == "Bearer old-token"
    assert transport.requests[1].headers["Authorization"] == (
        "Bearer replacement-token"
    )


@pytest.mark.asyncio
async def test_provider_preserves_second_401_error() -> None:
    issuer = SequenceIssuer(["one", "two"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport(
        [TossResponse(status_code=401), TossResponse(status_code=401)]
    )
    provider = TossMarketDataProvider(
        TossHttpClient(
            "https://example.test",
            transport=transport,
            token_manager=manager,
        ),
        clock=FixedClock(),
    )

    with pytest.raises(TossAuthenticationError):
        await provider.get_quote("005930")

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_naive_market_clock_is_rejected() -> None:
    provider, transport = provider_with_responses(
        [response_with_result([price_item()])],
        clock=FixedClock(datetime(2026, 7, 21, 12, 0)),
    )

    with pytest.raises(TossConfigurationError, match="clock"):
        await provider.get_quote("005930")

    assert transport.requests == []


@pytest.mark.asyncio
async def test_string_is_not_accepted_as_symbol_sequence() -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError, match="sequence"):
        await provider.get_quotes("AAPL")

    assert transport.requests == []


@pytest.mark.asyncio
async def test_more_than_200_requested_items_is_rejected_even_with_duplicates() -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError, match="more than 200"):
        await provider.get_quotes(["AAPL"] * 201)

    assert transport.requests == []


@pytest.mark.asyncio
async def test_nonboolean_adjusted_flag_is_rejected() -> None:
    provider, transport = provider_with_responses([])

    with pytest.raises(TossConfigurationError, match="boolean"):
        await provider.get_candles(
            "005930",
            CandleInterval.DAY_1,
            adjusted=cast(bool, 1),
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_candle_response_requires_next_before_field() -> None:
    provider, _ = provider_with_responses(
        [response_with_result({"candles": [candle_item("2026-07-21T09:00:00+09:00")]})]
    )

    with pytest.raises(TossInvalidResponseError, match="pagination"):
        await provider.get_candles("005930", CandleInterval.DAY_1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lastPrice", " 95000"),
        ("lastPrice", "95000 "),
        ("timestamp", " 2026-07-21T20:59:59+09:00"),
    ],
)
async def test_price_response_rejects_whitespace_padded_fields(
    field: str,
    value: object,
) -> None:
    item = price_item()
    item[field] = value
    provider, _ = provider_with_responses([response_with_result([item])])

    with pytest.raises(TossInvalidResponseError):
        await provider.get_quote("005930")
