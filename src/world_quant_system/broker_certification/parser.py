from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from world_quant_system.broker_certification.models import (
    BrokerAdapterConfigurationError,
    BrokerAdapterIntegrityError,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedOrder,
    ReadOnlyOperation,
    sha256_document,
)

_ACCOUNT_SEQ_KEYS = ("accountSeq", "account_seq", "accountId", "id")
_ACCOUNT_NAME_KEYS = ("accountName", "name", "displayName", "nickname")
_HOLDINGS_CONTAINER_KEYS = ("holdings", "items", "positions")
_ORDERS_CONTAINER_KEYS = ("orders", "items")
_SYMBOL_KEYS = ("symbol", "stockCode", "ticker")
_QUANTITY_KEYS = ("quantity", "holdingQuantity", "balanceQuantity")
_AVERAGE_PRICE_KEYS = (
    "averagePrice",
    "averagePurchasePrice",
    "purchaseAveragePrice",
)
_CURRENCY_KEYS = ("currency", "currencyCode")
_ORDER_ID_KEYS = ("orderId", "order_id", "id")
_CLIENT_ORDER_ID_KEYS = ("clientOrderId", "client_order_id")
_SIDE_KEYS = ("side", "orderSide")
_STATUS_KEYS = ("status", "orderStatus")
_FILLED_QUANTITY_KEYS = (
    "filledQuantity",
    "executedQuantity",
    "cumulatedQuantity",
)


class TossReadOnlyResponseParser:
    """Normalize read-only response captures without discarding raw evidence."""

    def parse_accounts(
        self,
        body: Mapping[str, object],
    ) -> tuple[NormalizedAccount, ...]:
        records = _result_records(body, operation=ReadOnlyOperation.ACCOUNTS)
        parsed: list[NormalizedAccount] = []
        for record in records:
            account_seq = _required_text(record, _ACCOUNT_SEQ_KEYS, "account sequence")
            account_name = _optional_text(record, _ACCOUNT_NAME_KEYS)
            parsed.append(
                NormalizedAccount(
                    account_seq=account_seq,
                    account_name=account_name,
                    raw_digest=sha256_document(record),
                )
            )
        return tuple(parsed)

    def parse_holdings(
        self,
        body: Mapping[str, object],
    ) -> tuple[NormalizedHolding, ...]:
        result = _result_value(body, operation=ReadOnlyOperation.HOLDINGS)
        records = _nested_or_direct_records(result, _HOLDINGS_CONTAINER_KEYS)
        parsed: list[NormalizedHolding] = []
        for record in records:
            parsed.append(
                NormalizedHolding(
                    symbol=_required_text(record, _SYMBOL_KEYS, "holding symbol"),
                    quantity=_required_scalar_text(
                        record,
                        _QUANTITY_KEYS,
                        "holding quantity",
                    ),
                    average_price=_optional_scalar_text(record, _AVERAGE_PRICE_KEYS),
                    currency=_optional_text(record, _CURRENCY_KEYS),
                    raw_digest=sha256_document(record),
                )
            )
        return tuple(parsed)

    def parse_orders(self, body: Mapping[str, object]) -> tuple[NormalizedOrder, ...]:
        result = _result_value(body, operation=ReadOnlyOperation.OPEN_ORDERS)
        records = _nested_or_direct_records(result, _ORDERS_CONTAINER_KEYS)
        parsed: list[NormalizedOrder] = []
        for record in records:
            parsed.append(
                NormalizedOrder(
                    order_id=_required_text(record, _ORDER_ID_KEYS, "order ID"),
                    client_order_id=_optional_text(record, _CLIENT_ORDER_ID_KEYS),
                    symbol=_required_text(record, _SYMBOL_KEYS, "order symbol"),
                    side=_required_text(record, _SIDE_KEYS, "order side").upper(),
                    status=_required_text(
                        record,
                        _STATUS_KEYS,
                        "order status",
                    ).upper(),
                    quantity=_required_scalar_text(
                        record,
                        _QUANTITY_KEYS,
                        "order quantity",
                    ),
                    filled_quantity=_optional_scalar_text(
                        record,
                        _FILLED_QUANTITY_KEYS,
                    ),
                    raw_digest=sha256_document(record),
                )
            )
        return tuple(parsed)


def extract_next_page_token(body: Mapping[str, object]) -> str | None:
    result = body.get("result")
    candidates: list[Mapping[str, object]] = [body]
    if isinstance(result, dict):
        candidates.append(result)
    for candidate in candidates:
        for key in ("nextCursor", "nextPageToken", "next", "cursor"):
            value = candidate.get(key)
            if value is None:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
            raise BrokerAdapterConfigurationError(
                f"Pagination token {key} must be nonblank text or null."
            )
    return None


def _result_records(
    body: Mapping[str, object],
    *,
    operation: ReadOnlyOperation,
) -> tuple[Mapping[str, object], ...]:
    result = _result_value(body, operation=operation)
    return _as_record_sequence(result, field_name=f"{operation.value} result")


def _result_value(
    body: Mapping[str, object],
    *,
    operation: ReadOnlyOperation,
) -> object:
    if "error" in body:
        raise BrokerAdapterIntegrityError(
            f"{operation.value} success response unexpectedly contains error envelope."
        )
    if "result" not in body:
        raise BrokerAdapterConfigurationError(
            f"{operation.value} response is missing result."
        )
    return body["result"]


def _nested_or_direct_records(
    result: object,
    container_keys: Sequence[str],
) -> tuple[Mapping[str, object], ...]:
    if isinstance(result, list):
        return _as_record_sequence(result, field_name="result")
    if not isinstance(result, dict):
        raise BrokerAdapterConfigurationError(
            "Response result must be a list or object."
        )
    for key in container_keys:
        if key in result:
            return _as_record_sequence(result[key], field_name=key)
    if _looks_like_record(result):
        return (result,)
    raise BrokerAdapterConfigurationError(
        "Response result does not contain a recognized item collection."
    )


def _as_record_sequence(
    value: object,
    *,
    field_name: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise BrokerAdapterConfigurationError(f"{field_name} must be a list.")
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BrokerAdapterConfigurationError(
                f"{field_name} items must be objects."
            )
        records.append(item)
    return tuple(records)


def _looks_like_record(value: Mapping[str, object]) -> bool:
    record_keys = set(value)
    aliases = set(_SYMBOL_KEYS) | set(_ORDER_ID_KEYS) | set(_ACCOUNT_SEQ_KEYS)
    return bool(record_keys & aliases)


def _required_text(
    record: Mapping[str, object],
    keys: Iterable[str],
    field_name: str,
) -> str:
    value = _first_present(record, keys)
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be nonblank text."
        )
    return value.strip()


def _optional_text(
    record: Mapping[str, object],
    keys: Iterable[str],
) -> str | None:
    value = _first_present(record, keys, missing=None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterConfigurationError(
            "Optional text field must be nonblank when present."
        )
    return value.strip()


def _required_scalar_text(
    record: Mapping[str, object],
    keys: Iterable[str],
    field_name: str,
) -> str:
    value = _first_present(record, keys)
    return _scalar_text(value, field_name=field_name)


def _optional_scalar_text(
    record: Mapping[str, object],
    keys: Iterable[str],
) -> str | None:
    value = _first_present(record, keys, missing=None)
    if value is None:
        return None
    return _scalar_text(value, field_name="optional numeric field")


def _scalar_text(value: object, *, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a string or number."
        )
    text = str(value).strip()
    if not text:
        raise BrokerAdapterConfigurationError(f"{field_name} cannot be blank.")
    return text


_MISSING = object()


def _first_present(
    record: Mapping[str, object],
    keys: Iterable[str],
    *,
    missing: object = _MISSING,
) -> object:
    for key in keys:
        if key in record:
            return record[key]
    if missing is not _MISSING:
        return missing
    raise BrokerAdapterConfigurationError(
        f"Required field is missing; accepted aliases: {', '.join(keys)}"
    )
