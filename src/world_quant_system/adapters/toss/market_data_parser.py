from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import NoReturn, cast

from world_quant_system.adapters.toss.errors import (
    TossConfigurationError,
    TossInvalidResponseError,
)
from world_quant_system.domain.models import (
    Candle,
    CandleInterval,
    CandlePage,
    Quote,
)


class TossMarketDataParser:
    """Convert untrusted Toss JSON payloads into validated domain models."""

    def __init__(
        self,
        *,
        source: str = "toss",
        future_tolerance: timedelta = timedelta(minutes=5),
    ) -> None:
        if not isinstance(source, str) or not source.strip():
            raise TossConfigurationError("Market-data source cannot be empty.")

        if not isinstance(future_tolerance, timedelta) or future_tolerance < timedelta(
            0
        ):
            raise TossConfigurationError(
                "Future timestamp tolerance cannot be negative."
            )

        self._source = source
        self._future_tolerance = future_tolerance

    def parse_quotes(
        self,
        json_body: Mapping[str, object] | None,
        *,
        expected_symbols: Sequence[str],
        now_utc: datetime,
    ) -> dict[str, Quote]:
        self._require_aware_datetime(now_utc, "Market-data clock")
        body = self._require_mapping(json_body, "price response")
        items = self._require_list(body.get("result"), "price result")

        expected = tuple(expected_symbols)
        expected_set = set(expected)
        parsed: dict[str, Quote] = {}

        for raw_item in items:
            item = self._require_mapping(raw_item, "price item")
            symbol = self._parse_symbol(item.get("symbol"))

            if symbol not in expected_set:
                self._invalid("Price response contained an unexpected symbol.")

            if symbol in parsed:
                self._invalid("Price response contained a duplicate symbol.")

            timestamp = self._parse_timestamp(
                item.get("timestamp"),
                field_name="Price timestamp",
                now_utc=now_utc,
            )
            price = self._parse_positive_decimal(
                item.get("lastPrice"),
                field_name="Last price",
            )
            currency = self._parse_currency(item.get("currency"))

            try:
                parsed[symbol] = Quote(
                    symbol=symbol,
                    price=price,
                    timestamp=timestamp,
                    currency=currency,
                    source=self._source,
                )
            except ValueError as error:
                raise TossInvalidResponseError(
                    "Price response violated a quote invariant."
                ) from error

        if set(parsed) != expected_set:
            self._invalid("Price response did not contain every requested symbol.")

        return parsed

    def parse_candle_page(
        self,
        json_body: Mapping[str, object] | None,
        *,
        symbol: str,
        interval: CandleInterval,
        requested_count: int,
        now_utc: datetime,
    ) -> CandlePage:
        self._require_aware_datetime(now_utc, "Market-data clock")

        if (
            isinstance(requested_count, bool)
            or not isinstance(requested_count, int)
            or not 1 <= requested_count <= 200
        ):
            raise TossConfigurationError(
                "Requested candle count must be between 1 and 200."
            )

        body = self._require_mapping(json_body, "candle response")
        result = self._require_mapping(body.get("result"), "candle result")
        raw_candles = self._require_list(result.get("candles"), "candles")

        if len(raw_candles) > requested_count:
            self._invalid("Candle response exceeded the requested count.")

        candles = [
            self._parse_candle(
                raw_candle,
                symbol=symbol,
                interval=interval,
                now_utc=now_utc,
            )
            for raw_candle in raw_candles
        ]

        self._validate_raw_candle_order(candles)
        candles.sort(key=lambda candle: candle.timestamp)

        if "nextBefore" not in result:
            self._invalid("Candle response omitted the pagination field.")

        next_before = self._parse_optional_timestamp(
            result.get("nextBefore"),
            field_name="Next-before timestamp",
            now_utc=now_utc,
        )

        if not candles and next_before is not None:
            self._invalid(
                "Empty candle response cannot include a pagination timestamp."
            )

        try:
            return CandlePage(
                candles=tuple(candles),
                next_before=next_before,
            )
        except ValueError as error:
            raise TossInvalidResponseError(
                "Candle response violated a page invariant."
            ) from error

    def _parse_candle(
        self,
        raw_candle: object,
        *,
        symbol: str,
        interval: CandleInterval,
        now_utc: datetime,
    ) -> Candle:
        item = self._require_mapping(raw_candle, "candle item")
        timestamp = self._parse_timestamp(
            item.get("timestamp"),
            field_name="Candle timestamp",
            now_utc=now_utc,
        )
        open_price = self._parse_positive_decimal(
            item.get("openPrice"),
            field_name="Open price",
        )
        high_price = self._parse_positive_decimal(
            item.get("highPrice"),
            field_name="High price",
        )
        low_price = self._parse_positive_decimal(
            item.get("lowPrice"),
            field_name="Low price",
        )
        close_price = self._parse_positive_decimal(
            item.get("closePrice"),
            field_name="Close price",
        )
        volume = self._parse_nonnegative_integer(
            item.get("volume"),
            field_name="Volume",
        )
        currency = self._parse_currency(item.get("currency"))

        try:
            return Candle(
                symbol=symbol,
                interval=interval,
                timestamp=timestamp,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                currency=currency,
                source=self._source,
            )
        except ValueError as error:
            raise TossInvalidResponseError(
                "Candle response violated an OHLCV invariant."
            ) from error

    @staticmethod
    def _validate_raw_candle_order(candles: Sequence[Candle]) -> None:
        if len(candles) < 2:
            return

        comparisons = [
            current.timestamp > previous.timestamp
            for previous, current in pairwise(candles)
        ]
        reverse_comparisons = [
            current.timestamp < previous.timestamp
            for previous, current in pairwise(candles)
        ]

        if all(comparisons) or all(reverse_comparisons):
            return

        raise TossInvalidResponseError(
            "Candle response timestamps were duplicated or non-monotonic."
        )

    def _parse_timestamp(
        self,
        value: object,
        *,
        field_name: str,
        now_utc: datetime,
    ) -> datetime:
        if not isinstance(value, str) or not value or value != value.strip():
            self._invalid(f"{field_name} was missing or invalid.")

        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as error:
            raise TossInvalidResponseError(
                f"{field_name} was not valid ISO 8601."
            ) from error

        self._require_aware_datetime(timestamp, field_name)

        if timestamp.astimezone(UTC) > (
            now_utc.astimezone(UTC) + self._future_tolerance
        ):
            self._invalid(f"{field_name} was too far in the future.")

        return timestamp

    def _parse_optional_timestamp(
        self,
        value: object,
        *,
        field_name: str,
        now_utc: datetime,
    ) -> datetime | None:
        if value is None:
            return None

        return self._parse_timestamp(
            value,
            field_name=field_name,
            now_utc=now_utc,
        )

    @staticmethod
    def _parse_symbol(value: object) -> str:
        if not isinstance(value, str):
            raise TossInvalidResponseError(
                "Price response symbol was missing or invalid."
            )

        if value != value.strip():
            raise TossInvalidResponseError(
                "Price response symbol was padded with whitespace."
            )

        symbol = value.upper()
        if not symbol:
            raise TossInvalidResponseError("Price response symbol was empty.")

        return symbol

    @staticmethod
    def _parse_currency(value: object) -> str:
        if not isinstance(value, str):
            raise TossInvalidResponseError(
                "Market-data currency was missing or invalid."
            )

        if value != value.strip():
            raise TossInvalidResponseError(
                "Market-data currency was padded with whitespace."
            )

        currency = value.upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise TossInvalidResponseError(
                "Market-data currency was not a three-letter code."
            )

        return currency

    @staticmethod
    def _parse_positive_decimal(
        value: object,
        *,
        field_name: str,
    ) -> Decimal:
        if not isinstance(value, str) or not value or value != value.strip():
            raise TossInvalidResponseError(f"{field_name} was missing or invalid.")

        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise TossInvalidResponseError(
                f"{field_name} was not a valid decimal."
            ) from error

        if not parsed.is_finite() or parsed <= Decimal("0"):
            raise TossInvalidResponseError(
                f"{field_name} must be a finite positive decimal."
            )

        return parsed

    @staticmethod
    def _parse_nonnegative_integer(
        value: object,
        *,
        field_name: str,
    ) -> int:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not value.isascii()
        ):
            raise TossInvalidResponseError(f"{field_name} was missing or invalid.")

        if not value.isdecimal():
            raise TossInvalidResponseError(
                f"{field_name} was not a nonnegative integer."
            )

        return int(value)

    @staticmethod
    def _require_mapping(
        value: object,
        field_name: str,
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise TossInvalidResponseError(f"Toss {field_name} was missing or invalid.")

        return cast(Mapping[str, object], value)

    @staticmethod
    def _require_list(
        value: object,
        field_name: str,
    ) -> list[object]:
        if not isinstance(value, list):
            raise TossInvalidResponseError(f"Toss {field_name} was missing or invalid.")

        return cast(list[object], value)

    @staticmethod
    def _require_aware_datetime(
        value: datetime,
        field_name: str,
    ) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise TossInvalidResponseError(f"{field_name} must be timezone-aware.")

    @staticmethod
    def _invalid(message: str) -> NoReturn:
        raise TossInvalidResponseError(message)
