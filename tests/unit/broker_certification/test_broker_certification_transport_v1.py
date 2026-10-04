from __future__ import annotations

from datetime import UTC, datetime

import pytest

from world_quant_system.broker_certification.models import (
    BrokerAdapterSafetyError,
    ReadOnlyOperation,
    ReadOnlyRequest,
    ReadOnlyResponse,
)
from world_quant_system.broker_certification.transport import (
    FixtureReadOnlyTransport,
    NoNetworkReadOnlyTransport,
    RetryingReadOnlyClient,
)


def _request() -> ReadOnlyRequest:
    return ReadOnlyRequest(
        operation=ReadOnlyOperation.ACCOUNTS,
        url="https://openapi.tossinvest.com/api/v1/accounts",
        headers=(("Authorization", "Bearer fixture"),),
    )


def _response(status_code: int, *, retry_after: str | None = None) -> ReadOnlyResponse:
    headers = [("X-Request-Id", f"req-{status_code}")]
    if retry_after is not None:
        headers.append(("Retry-After", retry_after))
    return ReadOnlyResponse(
        status_code=status_code,
        headers=tuple(headers),
        body={"result": []} if status_code == 200 else {"error": {"code": "x"}},
        received_at=datetime(2026, 8, 3, tzinfo=UTC),
        raw_size_bytes=10,
    )


@pytest.mark.asyncio
async def test_no_network_transport_fails_closed() -> None:
    with pytest.raises(BrokerAdapterSafetyError, match="Network transport"):
        await NoNetworkReadOnlyTransport().send(_request())


@pytest.mark.asyncio
async def test_retry_client_retries_429_then_succeeds() -> None:
    transport = FixtureReadOnlyTransport(
        {
            ReadOnlyOperation.ACCOUNTS: (
                _response(429, retry_after="0"),
                _response(200),
            )
        }
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = RetryingReadOnlyClient(
        transport,
        maximum_attempts=3,
        base_backoff_seconds=0.1,
        maximum_backoff_seconds=1.0,
        sleep=fake_sleep,
    )
    response = await client.get(_request())
    assert response.status_code == 200
    assert len(transport.calls) == 2
    assert delays == [0.0]
    assert transport.calls[0]["headers"] == {"authorization": "<redacted>"}


@pytest.mark.asyncio
async def test_retry_client_respects_attempt_budget() -> None:
    transport = FixtureReadOnlyTransport(
        {ReadOnlyOperation.ACCOUNTS: (_response(500), _response(500))}
    )

    async def fake_sleep(delay: float) -> None:
        del delay

    client = RetryingReadOnlyClient(
        transport,
        maximum_attempts=2,
        base_backoff_seconds=0,
        maximum_backoff_seconds=0,
        sleep=fake_sleep,
    )
    response = await client.get(_request())
    assert response.status_code == 500
    assert len(transport.calls) == 2
