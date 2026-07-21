import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from world_quant_system.adapters.toss.client import TossHttpClient
from world_quant_system.adapters.toss.errors import TossConfigurationError
from world_quant_system.adapters.toss.market_data_parser import (
    TossMarketDataParser,
)
from world_quant_system.adapters.toss.schemas import TossResponse
from world_quant_system.broker.market_data import (
    CandleDataProvider,
    MarketDataProvider,
)
from world_quant_system.data.raw_market_data import (
    RawMarketDataCapture,
    RawMarketDataRecorder,
)
from world_quant_system.domain.models import (
    CandleInterval,
    CandlePage,
    Quote,
)

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]+$")
_MAX_SYMBOLS_PER_PRICE_REQUEST = 200
_MAX_CANDLES_PER_REQUEST = 200


class MarketDataClock(Protocol):
    def now_utc(self) -> datetime:
        """Return a timezone-aware current time."""
        ...


class SystemMarketDataClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class TossMarketDataProvider(
    MarketDataProvider,
    CandleDataProvider,
):
    """Read-only market data backed by validated Toss API responses."""

    def __init__(
        self,
        client: TossHttpClient,
        *,
        clock: MarketDataClock | None = None,
        future_tolerance: timedelta = timedelta(minutes=5),
        raw_recorder: RawMarketDataRecorder | None = None,
    ) -> None:
        if not isinstance(future_tolerance, timedelta) or future_tolerance < timedelta(
            0
        ):
            raise TossConfigurationError(
                "Future timestamp tolerance cannot be negative."
            )

        self._client = client
        self._clock = clock or SystemMarketDataClock()
        self._parser = TossMarketDataParser(
            future_tolerance=future_tolerance,
        )
        self._future_tolerance = future_tolerance
        self._raw_recorder = raw_recorder

    async def get_quote(self, symbol: str) -> Quote:
        quotes = await self.get_quotes([symbol])
        return quotes[0]

    async def get_quotes(
        self,
        symbols: Sequence[str],
    ) -> list[Quote]:
        if isinstance(symbols, str):
            raise TossConfigurationError(
                "Symbols must be provided as a sequence of symbol strings."
            )

        requested = tuple(self._normalize_symbol(symbol) for symbol in symbols)
        if not requested:
            return []

        if len(requested) > _MAX_SYMBOLS_PER_PRICE_REQUEST:
            raise TossConfigurationError(
                "A price request cannot contain more than 200 symbols."
            )

        unique_symbols = tuple(dict.fromkeys(requested))
        now_utc = self._validated_now_utc()
        endpoint = "/api/v1/prices"
        params = {"symbols": ",".join(unique_symbols)}
        response = await self._client.get(
            endpoint,
            params=params,
        )
        await self._record_raw_response(
            endpoint=endpoint,
            params=params,
            response=response,
        )
        parsed = self._parser.parse_quotes(
            response.json_body,
            expected_symbols=unique_symbols,
            now_utc=now_utc,
        )
        return [parsed[symbol] for symbol in requested]

    async def get_candles(
        self,
        symbol: str,
        interval: CandleInterval,
        *,
        count: int = 100,
        before: datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        normalized_symbol = self._normalize_symbol(symbol)

        if not isinstance(interval, CandleInterval):
            raise TossConfigurationError(
                "Candle interval must be a CandleInterval value."
            )

        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= _MAX_CANDLES_PER_REQUEST
        ):
            raise TossConfigurationError("Candle count must be between 1 and 200.")

        if not isinstance(adjusted, bool):
            raise TossConfigurationError("Candle adjusted flag must be a boolean.")

        now_utc = self._validated_now_utc()
        if before is not None:
            self._validate_before(before, now_utc)

        params = {
            "symbol": normalized_symbol,
            "interval": interval.value,
            "count": str(count),
            "adjusted": "true" if adjusted else "false",
        }
        if before is not None:
            params["before"] = before.isoformat()

        endpoint = "/api/v1/candles"
        response = await self._client.get(
            endpoint,
            params=params,
        )
        await self._record_raw_response(
            endpoint=endpoint,
            params=params,
            response=response,
        )
        return self._parser.parse_candle_page(
            response.json_body,
            symbol=normalized_symbol,
            interval=interval,
            requested_count=count,
            now_utc=now_utc,
        )

    async def _record_raw_response(
        self,
        *,
        endpoint: str,
        params: dict[str, str],
        response: object,
    ) -> None:
        recorder = self._raw_recorder
        if recorder is None:
            return

        if not isinstance(response, TossResponse):
            raise TossConfigurationError(
                "Market-data client returned an unsupported response type."
            )

        request_id = self._get_header(response.headers, "X-Request-Id")
        if request_id is not None:
            request_id = request_id.strip()
            if not request_id or len(request_id) > 512:
                request_id = None

        idempotency_key = None
        if request_id is not None and len(request_id) <= 507:
            idempotency_key = f"toss:{request_id}"

        await recorder.record(
            RawMarketDataCapture(
                provider="toss",
                endpoint=endpoint,
                request_params=params,
                captured_at=self._validated_now_utc(),
                status_code=response.status_code,
                response_headers=response.headers,
                json_body=response.json_body,
                text=response.text,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        )

    @staticmethod
    def _get_header(
        headers: Mapping[str, str],
        expected_name: str,
    ) -> str | None:
        normalized_expected_name = expected_name.casefold()
        for name, value in headers.items():
            if name.casefold() == normalized_expected_name:
                return value
        return None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        if not isinstance(symbol, str):
            raise TossConfigurationError("Symbol must be a string.")

        normalized = symbol.strip().upper()
        if not normalized:
            raise TossConfigurationError("Symbol cannot be empty.")

        if not _SYMBOL_PATTERN.fullmatch(normalized):
            raise TossConfigurationError("Symbol contains unsupported characters.")

        return normalized

    def _validate_before(
        self,
        before: datetime,
        now_utc: datetime,
    ) -> None:
        if (
            not isinstance(before, datetime)
            or before.tzinfo is None
            or before.utcoffset() is None
        ):
            raise TossConfigurationError(
                "Candle before timestamp must be timezone-aware."
            )

        if before.astimezone(UTC) > (now_utc.astimezone(UTC) + self._future_tolerance):
            raise TossConfigurationError(
                "Candle before timestamp is too far in the future."
            )

    def _validated_now_utc(self) -> datetime:
        now = self._clock.now_utc()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise TossConfigurationError(
                "Market-data clock must return a timezone-aware datetime."
            )

        return now.astimezone(UTC)
