from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from world_quant_system.toss_live_read.models import (
    LiveReadOperation,
    TossLiveReadIntegrityError,
    TossLiveReadPolicy,
)
from world_quant_system.toss_live_read.parser import (
    build_orders_evidence,
    parse_account_evidence,
    parse_holdings_evidence,
    parse_order_page,
)


def test_policy_is_strictly_read_only() -> None:
    policy = TossLiveReadPolicy()
    assert policy.broker_writes_enabled is False
    assert policy.redirects_enabled is False
    assert policy.proxy_inheritance_enabled is False


def test_account_parser_never_exposes_account_number() -> None:
    evidence = parse_account_evidence(
        {
            "result": [
                {
                    "accountNo": "12345678901",
                    "accountSeq": 7,
                    "accountType": "BROKERAGE",
                }
            ]
        },
        expected_account_seq=7,
        required_account_type="BROKERAGE",
    )
    document = evidence.to_document()
    assert "12345678901" not in str(document)
    assert document["account_fingerprint"] == hashlib.sha256(b"7").hexdigest()


def test_holdings_parser_rejects_duplicate_symbol() -> None:
    item = {
        "symbol": "005930",
        "currency": "KRW",
        "quantity": "1",
        "lastPrice": "10",
        "averagePurchasePrice": "9",
    }
    with pytest.raises(TossLiveReadIntegrityError, match="Duplicate"):
        parse_holdings_evidence({"result": {"items": [item, item]}})


def test_order_parser_rejects_unknown_status() -> None:
    document = {
        "result": {
            "orders": [
                {
                    "orderId": "order-1",
                    "symbol": "005930",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "NEW_UNKNOWN_STATE",
                    "currency": "KRW",
                    "quantity": "1",
                    "orderAmount": None,
                    "orderedAt": datetime.now(UTC).isoformat(),
                    "execution": {"filledQuantity": "0"},
                }
            ],
            "nextCursor": None,
            "hasNext": False,
        }
    }
    with pytest.raises(TossLiveReadIntegrityError, match="Unknown broker"):
        parse_order_page(document, operation=LiveReadOperation.OPEN_ORDERS)


def test_order_evidence_rejects_duplicate_ids_across_pages() -> None:
    order = {
        "order_id": "same",
        "symbol": "005930",
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "status": "FILLED",
        "currency": "KRW",
        "quantity": "1",
        "filled_quantity": "1",
        "ordered_at": "2026-01-01T00:00:00+09:00",
    }
    with pytest.raises(TossLiveReadIntegrityError, match="Duplicate order"):
        build_orders_evidence(
            operation=LiveReadOperation.CLOSED_ORDERS,
            pages=[[order], [order]],
        )


def test_policy_cannot_rename_live_enable_gate() -> None:
    with pytest.raises(Exception, match="enable variable"):
        TossLiveReadPolicy(enable_variable="WEAK_GATE")
