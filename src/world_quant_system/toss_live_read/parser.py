from __future__ import annotations

from decimal import Decimal, InvalidOperation

from world_quant_system.toss_live_read.models import (
    AccountEvidence,
    CollectionEvidence,
    LiveReadOperation,
    TossLiveReadIntegrityError,
    digest_document,
    sha256_secret,
)

_ALLOWED_ORDER_STATES = frozenset(
    {
        "PENDING",
        "PARTIAL_FILLED",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "REPLACED",
        "CANCEL_REJECTED",
        "REPLACE_REJECTED",
    }
)
_ALLOWED_ORDER_SIDES = frozenset({"BUY", "SELL"})
_ALLOWED_ORDER_TYPES = frozenset({"LIMIT", "MARKET"})
_ALLOWED_TIME_IN_FORCE = frozenset({"DAY", "CLS"})
_ALLOWED_CURRENCIES = frozenset({"KRW", "USD"})


def parse_account_evidence(
    document: object,
    *,
    expected_account_seq: int,
    required_account_type: str,
) -> AccountEvidence:
    result = _result(document)
    if not isinstance(result, list):
        raise TossLiveReadIntegrityError("Accounts result must be an array.")
    matched: list[dict[object, object]] = []
    sanitized: list[dict[str, object]] = []
    for raw_item in result:
        if not isinstance(raw_item, dict):
            raise TossLiveReadIntegrityError("Account item must be an object.")
        account_seq = raw_item.get("accountSeq")
        account_type = raw_item.get("accountType")
        account_no = raw_item.get("accountNo")
        if isinstance(account_seq, bool) or not isinstance(account_seq, int):
            raise TossLiveReadIntegrityError("accountSeq must be an integer.")
        if not isinstance(account_type, str) or not account_type:
            raise TossLiveReadIntegrityError("accountType must be text.")
        if not isinstance(account_no, str) or not account_no.strip():
            raise TossLiveReadIntegrityError("accountNo must be nonblank text.")
        sanitized.append(
            {
                "account_fingerprint": sha256_secret(str(account_seq).encode()),
                "account_type": account_type,
            }
        )
        if account_seq == expected_account_seq:
            matched.append(raw_item)
    if len(matched) != 1:
        raise TossLiveReadIntegrityError(
            "Configured accountSeq must match exactly one returned account."
        )
    selected_type = matched[0].get("accountType")
    if selected_type != required_account_type:
        raise TossLiveReadIntegrityError(
            "Configured account is not the required BROKERAGE account type."
        )
    return AccountEvidence(
        account_fingerprint=sha256_secret(str(expected_account_seq).encode()),
        account_type=required_account_type,
        returned_account_count=len(result),
        payload_digest=digest_document(sorted(sanitized, key=str)),
    )


def parse_holdings_evidence(document: object) -> CollectionEvidence:
    result = _result(document)
    if not isinstance(result, dict):
        raise TossLiveReadIntegrityError("Holdings result must be an object.")
    items = result.get("items")
    if not isinstance(items, list):
        raise TossLiveReadIntegrityError("Holdings items must be an array.")
    normalized: list[dict[str, str]] = []
    seen_symbols: set[str] = set()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise TossLiveReadIntegrityError("Holding item must be an object.")
        symbol = _nonblank_text(raw_item.get("symbol"), "Holding symbol").upper()
        currency = _nonblank_text(raw_item.get("currency"), "Holding currency")
        if currency not in _ALLOWED_CURRENCIES:
            raise TossLiveReadIntegrityError("Unknown holding currency.")
        quantity = _nonnegative_decimal(raw_item.get("quantity"), "Holding quantity")
        last_price = _nonnegative_decimal(raw_item.get("lastPrice"), "Last price")
        average_price = _nonnegative_decimal(
            raw_item.get("averagePurchasePrice"),
            "Average purchase price",
        )
        if symbol in seen_symbols:
            raise TossLiveReadIntegrityError("Duplicate holding symbol detected.")
        seen_symbols.add(symbol)
        normalized.append(
            {
                "symbol": symbol,
                "currency": currency,
                "quantity": str(quantity),
                "last_price": str(last_price),
                "average_purchase_price": str(average_price),
            }
        )
    normalized.sort(key=lambda value: (value["currency"], value["symbol"]))
    return CollectionEvidence(
        operation=LiveReadOperation.HOLDINGS,
        item_count=len(normalized),
        page_count=1,
        content_digest=digest_document(normalized),
    )


def parse_order_page(
    document: object,
    *,
    operation: LiveReadOperation,
) -> tuple[list[dict[str, str]], str | None, bool]:
    if operation not in {
        LiveReadOperation.OPEN_ORDERS,
        LiveReadOperation.CLOSED_ORDERS,
    }:
        raise TossLiveReadIntegrityError("Order page operation is invalid.")
    result = _result(document)
    if not isinstance(result, dict):
        raise TossLiveReadIntegrityError("Orders result must be an object.")
    orders = result.get("orders")
    next_cursor = result.get("nextCursor")
    has_next = result.get("hasNext")
    if not isinstance(orders, list):
        raise TossLiveReadIntegrityError("Orders must be an array.")
    if next_cursor is not None and not isinstance(next_cursor, str):
        raise TossLiveReadIntegrityError("Order cursor must be text or null.")
    if not isinstance(has_next, bool):
        raise TossLiveReadIntegrityError("Order hasNext must be boolean.")
    if has_next and (next_cursor is None or not next_cursor.strip()):
        raise TossLiveReadIntegrityError(
            "Next cursor is required when hasNext is true."
        )
    if not has_next and next_cursor is not None:
        raise TossLiveReadIntegrityError("Final order page cannot expose a cursor.")
    normalized: list[dict[str, str]] = []
    for raw_order in orders:
        normalized.append(_normalize_order(raw_order))
    return normalized, next_cursor, has_next


def build_orders_evidence(
    *,
    operation: LiveReadOperation,
    pages: list[list[dict[str, str]]],
) -> CollectionEvidence:
    flattened = [order for page in pages for order in page]
    order_ids = [order["order_id"] for order in flattened]
    if len(set(order_ids)) != len(order_ids):
        raise TossLiveReadIntegrityError("Duplicate order ID detected across pages.")
    flattened.sort(key=lambda value: (value["ordered_at"], value["order_id"]))
    states = tuple(sorted({order["status"] for order in flattened}))
    return CollectionEvidence(
        operation=operation,
        item_count=len(flattened),
        page_count=len(pages),
        content_digest=digest_document(flattened),
        observed_states=states,
    )


def _normalize_order(raw_order: object) -> dict[str, str]:
    if not isinstance(raw_order, dict):
        raise TossLiveReadIntegrityError("Order item must be an object.")
    order_id = _nonblank_text(raw_order.get("orderId"), "Order ID")
    symbol = _nonblank_text(raw_order.get("symbol"), "Order symbol").upper()
    side = _nonblank_text(raw_order.get("side"), "Order side")
    order_type = _nonblank_text(raw_order.get("orderType"), "Order type")
    time_in_force = _nonblank_text(
        raw_order.get("timeInForce"),
        "Time in force",
    )
    status = _nonblank_text(raw_order.get("status"), "Order status")
    currency = _nonblank_text(raw_order.get("currency"), "Order currency")
    ordered_at = _nonblank_text(raw_order.get("orderedAt"), "Ordered timestamp")
    if side not in _ALLOWED_ORDER_SIDES:
        raise TossLiveReadIntegrityError("Unknown order side.")
    if order_type not in _ALLOWED_ORDER_TYPES:
        raise TossLiveReadIntegrityError("Unknown order type.")
    if time_in_force not in _ALLOWED_TIME_IN_FORCE:
        raise TossLiveReadIntegrityError("Unknown time-in-force value.")
    if status not in _ALLOWED_ORDER_STATES:
        raise TossLiveReadIntegrityError("Unknown broker order status.")
    if currency not in _ALLOWED_CURRENCIES:
        raise TossLiveReadIntegrityError("Unknown order currency.")
    quantity_value = raw_order.get("quantity")
    order_amount_value = raw_order.get("orderAmount")
    if quantity_value is None and order_amount_value is None:
        raise TossLiveReadIntegrityError(
            "Order must contain quantity or orderAmount."
        )
    quantity = (
        _nonnegative_decimal(quantity_value, "Order quantity")
        if quantity_value is not None
        else Decimal("0")
    )
    execution = raw_order.get("execution")
    if not isinstance(execution, dict):
        raise TossLiveReadIntegrityError("Order execution must be an object.")
    filled_quantity = _nonnegative_decimal(
        execution.get("filledQuantity"),
        "Filled quantity",
    )
    if quantity_value is not None and filled_quantity > quantity:
        raise TossLiveReadIntegrityError(
            "Filled quantity cannot exceed requested quantity."
        )
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "status": status,
        "currency": currency,
        "quantity": str(quantity),
        "filled_quantity": str(filled_quantity),
        "ordered_at": ordered_at,
    }


def _result(document: object) -> object:
    if not isinstance(document, dict) or "result" not in document:
        raise TossLiveReadIntegrityError("Response must contain result.")
    return document["result"]


def _nonblank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TossLiveReadIntegrityError(f"{field_name} must be nonblank text.")
    return value


def _nonnegative_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise TossLiveReadIntegrityError(f"{field_name} must be decimal text.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise TossLiveReadIntegrityError(f"{field_name} is invalid.") from error
    if not parsed.is_finite() or parsed < 0:
        raise TossLiveReadIntegrityError(
            f"{field_name} must be finite and nonnegative."
        )
    return parsed
