from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from world_quant_system.broker_certification.models import (
    OFFICIAL_BASE_URL,
    BrokerAdapterSafetyError,
    ReadOnlyAdapterPolicy,
    ReadOnlyHttpMethod,
    ReadOnlyOperation,
    ReadOnlyRequest,
)


def _fingerprint(account_seq: str = "1") -> str:
    return hashlib.sha256(account_seq.encode()).hexdigest()


def _policy() -> ReadOnlyAdapterPolicy:
    return ReadOnlyAdapterPolicy(
        provider="TOSSINVEST",
        base_url=OFFICIAL_BASE_URL,
        expected_account_fingerprint=_fingerprint(),
        required_operations=(ReadOnlyOperation.ACCOUNTS,),
    )


def test_policy_rejects_nonofficial_host() -> None:
    with pytest.raises(BrokerAdapterSafetyError, match="official Toss"):
        ReadOnlyAdapterPolicy(
            provider="TOSSINVEST",
            base_url="https://example.com",
            expected_account_fingerprint=_fingerprint(),
            required_operations=(ReadOnlyOperation.ACCOUNTS,),
        )


def test_accounts_request_requires_get_and_no_account_header() -> None:
    request = ReadOnlyRequest(
        operation=ReadOnlyOperation.ACCOUNTS,
        url="https://openapi.tossinvest.com/api/v1/accounts",
        headers=(("Authorization", "Bearer fixture"),),
    )
    assert request.method is ReadOnlyHttpMethod.GET
    assert request.safe_document["headers"] == {"authorization": "<redacted>"}


def test_holdings_request_requires_account_header() -> None:
    with pytest.raises(BrokerAdapterSafetyError, match="X-Tossinvest-Account"):
        ReadOnlyRequest(
            operation=ReadOnlyOperation.HOLDINGS,
            url="https://openapi.tossinvest.com/api/v1/holdings",
            headers=(("Authorization", "Bearer fixture"),),
        )


def test_request_rejects_write_path() -> None:
    with pytest.raises(BrokerAdapterSafetyError, match="Unexpected path"):
        ReadOnlyRequest(
            operation=ReadOnlyOperation.OPEN_ORDERS,
            url="https://openapi.tossinvest.com/api/v1/orders/abc/cancel?status=OPEN",
            headers=(
                ("Authorization", "Bearer fixture"),
                ("X-Tossinvest-Account", "1"),
            ),
        )


def test_request_rejects_body() -> None:
    with pytest.raises(BrokerAdapterSafetyError, match="cannot contain a body"):
        ReadOnlyRequest(
            operation=ReadOnlyOperation.ACCOUNTS,
            url="https://openapi.tossinvest.com/api/v1/accounts",
            headers=(("Authorization", "Bearer fixture"),),
            body=b"{}",
        )


def test_policy_digest_is_deterministic() -> None:
    assert _policy().policy_digest == _policy().policy_digest
    assert len(_policy().policy_digest) == 64


def test_datetime_is_utc_aware_in_fixture() -> None:
    assert datetime(2026, 8, 3, tzinfo=UTC).utcoffset() is not None
