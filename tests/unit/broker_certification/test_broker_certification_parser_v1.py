from __future__ import annotations

import pytest

from world_quant_system.broker_certification.models import (
    BrokerAdapterConfigurationError,
)
from world_quant_system.broker_certification.parser import (
    TossReadOnlyResponseParser,
    extract_next_page_token,
)


def test_parser_normalizes_accounts_holdings_and_orders() -> None:
    parser = TossReadOnlyResponseParser()
    accounts = parser.parse_accounts(
        {"result": [{"accountSeq": "1", "accountName": "Primary"}]}
    )
    holdings = parser.parse_holdings(
        {
            "result": {
                "holdings": [
                    {
                        "symbol": "005930",
                        "quantity": "3",
                        "averagePrice": "90000",
                        "currency": "KRW",
                    }
                ]
            }
        }
    )
    orders = parser.parse_orders(
        {
            "result": {
                "orders": [
                    {
                        "orderId": "order-1",
                        "clientOrderId": "client-1",
                        "symbol": "005930",
                        "side": "buy",
                        "status": "filled",
                        "quantity": "1",
                        "filledQuantity": "1",
                    }
                ]
            }
        }
    )
    assert accounts[0].account_seq == "1"
    assert holdings[0].symbol == "005930"
    assert orders[0].status == "FILLED"


def test_parser_accepts_alias_fields() -> None:
    parser = TossReadOnlyResponseParser()
    holding = parser.parse_holdings(
        {
            "result": {
                "positions": [
                    {
                        "ticker": "AAPL",
                        "holdingQuantity": 2,
                        "averagePurchasePrice": 190.5,
                        "currencyCode": "USD",
                    }
                ]
            }
        }
    )[0]
    assert holding.quantity == "2"
    assert holding.average_price == "190.5"


def test_parser_rejects_missing_required_field() -> None:
    parser = TossReadOnlyResponseParser()
    with pytest.raises(BrokerAdapterConfigurationError, match="Required field"):
        parser.parse_accounts({"result": [{"accountName": "Primary"}]})


def test_extract_next_page_token() -> None:
    assert extract_next_page_token({"result": {"nextCursor": "abc"}}) == "abc"
    assert extract_next_page_token({"result": []}) is None
