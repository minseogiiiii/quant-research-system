from collections.abc import Sequence

import pytest

from world_quant_system.adapters.toss.client import TossHttpClient
from world_quant_system.adapters.toss.errors import (
    TossAdapterError,
    TossAuthenticationError,
    TossAuthorizationError,
    TossClientResponseError,
    TossConfigurationError,
    TossInvalidResponseError,
    TossRateLimitError,
    TossServerResponseError,
    TossTransportError,
)
from world_quant_system.adapters.toss.schemas import (
    HttpMethod,
    TossRequest,
    TossResponse,
)


class StubTransport:
    def __init__(
        self,
        responses: Sequence[TossResponse],
    ) -> None:
        self._responses = list(responses)
        self.requests: list[TossRequest] = []

    async def send(
        self,
        request: TossRequest,
    ) -> TossResponse:
        self.requests.append(request)

        if not self._responses:
            raise RuntimeError("No stub response remains.")

        return self._responses.pop(0)


class FailingTransport:
    async def send(
        self,
        request: TossRequest,
    ) -> TossResponse:
        del request
        raise RuntimeError("socket failure")


@pytest.mark.asyncio
async def test_default_transport_blocks_network() -> None:
    client = TossHttpClient(base_url="https://example.test")

    with pytest.raises(
        TossTransportError,
        match="Network transport is not configured",
    ):
        await client.get("/prices")


@pytest.mark.asyncio
async def test_client_prepares_get_request() -> None:
    transport = StubTransport(
        [
            TossResponse(
                status_code=200,
                json_body={"price": "95000"},
            )
        ]
    )
    client = TossHttpClient(
        base_url="https://example.test/",
        transport=transport,
        default_headers={"Accept": "application/json"},
    )

    response = await client.get(
        "prices",
        headers={"X-Test": "yes"},
        params={"symbol": "005930"},
    )

    assert response.status_code == 200
    assert len(transport.requests) == 1

    request = transport.requests[0]
    assert request.method == HttpMethod.GET
    assert request.url == "https://example.test/prices"
    assert request.headers == {
        "Accept": "application/json",
        "X-Test": "yes",
    }
    assert request.params == {"symbol": "005930"}


@pytest.mark.asyncio
async def test_request_headers_override_defaults() -> None:
    transport = StubTransport([TossResponse(status_code=204)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        default_headers={"Accept": "application/json"},
    )

    await client.get(
        "/prices",
        headers={"Accept": "application/problem+json"},
    )

    assert transport.requests[0].headers["Accept"] == ("application/problem+json")


@pytest.mark.asyncio
async def test_client_prepares_post_request() -> None:
    transport = StubTransport([TossResponse(status_code=201)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
    )

    await client.post(
        "/token",
        json_body={"grant_type": "client_credentials"},
    )

    request = transport.requests[0]
    assert request.method == HttpMethod.POST
    assert request.url == "https://example.test/token"
    assert request.json_body == {"grant_type": "client_credentials"}


@pytest.mark.asyncio
async def test_unexpected_transport_failure_is_wrapped() -> None:
    client = TossHttpClient(
        "https://example.test",
        transport=FailingTransport(),
    )

    with pytest.raises(
        TossTransportError,
        match="Transport failed unexpectedly",
    ) as captured:
        await client.get("/prices")

    assert isinstance(captured.value.__cause__, RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, TossAuthenticationError),
        (403, TossAuthorizationError),
        (400, TossClientResponseError),
        (404, TossClientResponseError),
        (500, TossServerResponseError),
        (503, TossServerResponseError),
        (199, TossInvalidResponseError),
        (600, TossInvalidResponseError),
    ],
)
async def test_client_maps_error_statuses(
    status_code: int,
    error_type: type[TossAdapterError],
) -> None:
    transport = StubTransport(
        [
            TossResponse(
                status_code=status_code,
                headers={"X-Request-Id": "request-1"},
            )
        ]
    )
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
    )

    with pytest.raises(error_type) as captured:
        await client.get("/resource")

    error = captured.value
    assert error.status_code == status_code
    assert error.request_id == "request-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("9", 9),
        ("0", 0),
        ("-1", None),
        ("not-a-number", None),
    ],
)
async def test_client_maps_rate_limit_response(
    header_value: str,
    expected: int | None,
) -> None:
    transport = StubTransport(
        [
            TossResponse(
                status_code=429,
                headers={
                    "retry-after": header_value,
                    "x-request-id": "rate-1",
                },
            )
        ]
    )
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
    )

    with pytest.raises(TossRateLimitError) as captured:
        await client.get("/prices")

    assert captured.value.retry_after_seconds == expected
    assert captured.value.request_id == "rate-1"


def test_client_requires_https() -> None:
    with pytest.raises(
        TossConfigurationError,
        match="must use HTTPS",
    ):
        TossHttpClient(base_url="http://example.test")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "   "])
async def test_client_rejects_empty_path(path: str) -> None:
    client = TossHttpClient(base_url="https://example.test")

    with pytest.raises(
        TossConfigurationError,
        match="cannot be empty",
    ):
        await client.get(path)


@pytest.mark.asyncio
async def test_client_rejects_full_url_path() -> None:
    client = TossHttpClient(base_url="https://example.test")

    with pytest.raises(
        TossConfigurationError,
        match="must not contain a full URL",
    ):
        await client.get("https://malicious.example/prices")


@pytest.mark.asyncio
async def test_request_headers_override_defaults_case_insensitively() -> None:
    transport = StubTransport([TossResponse(status_code=204)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        default_headers={"Accept": "application/json"},
    )

    await client.get(
        "/prices",
        headers={"accept": "application/problem+json"},
    )

    assert transport.requests[0].headers == {"accept": "application/problem+json"}
