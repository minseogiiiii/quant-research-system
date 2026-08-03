from __future__ import annotations

from decimal import Decimal

import pytest

from world_quant_system.order_write_certification.models import (
    OFFICIAL_ORDER_CREATE_PATH,
    OFFICIAL_TOSS_BASE_URL,
    OrderWriteSafetyError,
    TossOrderCreateContract,
    TossOrderMarket,
    TossOrderWritePolicy,
    validate_price_precision,
)


def test_contract_is_pinned_and_networkless() -> None:
    contract = TossOrderCreateContract()

    assert contract.base_url == OFFICIAL_TOSS_BASE_URL
    assert contract.path == OFFICIAL_ORDER_CREATE_PATH
    assert contract.method == "POST"
    assert contract.network_transport_enabled is False
    assert len(contract.contract_digest) == 64


def test_kr_and_us_price_precision_rules() -> None:
    assert validate_price_precision(
        price=Decimal("95000"),
        market=TossOrderMarket.KR,
    )
    assert not validate_price_precision(
        price=Decimal("95000.5"),
        market=TossOrderMarket.KR,
    )
    assert validate_price_precision(
        price=Decimal("0.1234"),
        market=TossOrderMarket.US,
    )
    assert not validate_price_precision(
        price=Decimal("0.12345"),
        market=TossOrderMarket.US,
    )
    assert validate_price_precision(
        price=Decimal("185.50"),
        market=TossOrderMarket.US,
    )
    assert not validate_price_precision(
        price=Decimal("185.501"),
        market=TossOrderMarket.US,
    )


def test_policy_forbids_maximum_at_high_value_threshold() -> None:
    with pytest.raises(OrderWriteSafetyError):
        TossOrderWritePolicy(
            expected_account_fingerprint="a" * 64,
            allowed_symbols=("005930",),
            currency="KRW",
            market=TossOrderMarket.KR,
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            maximum_certification_age_seconds=3600,
            maximum_order_quantity=10,
            maximum_order_notional=Decimal("100000000"),
            maximum_position_quantity=100,
            maximum_open_orders=5,
            minimum_cash_reserve_fraction=Decimal("0.1"),
            maximum_limit_deviation_bps=Decimal("100"),
            price_tick=Decimal("100"),
            human_approval_ttl_seconds=300,
            high_value_order_threshold=Decimal("100000000"),
        )
