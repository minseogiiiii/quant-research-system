import asyncio
from collections.abc import Sequence

import pytest

from world_quant_system.adapters.toss.client import TossHttpClient
from world_quant_system.adapters.toss.errors import (
    TossAuthenticationError,
    TossAuthorizationError,
    TossConfigurationError,
    TossTransportError,
)
from world_quant_system.adapters.toss.schemas import TossRequest, TossResponse
from world_quant_system.adapters.toss.token import TokenIssueResponse
from world_quant_system.adapters.toss.token_manager import TossTokenManager


class SequenceIssuer:
    def __init__(self, results: Sequence[str | Exception]) -> None:
        self._results = list(results)
        self.call_count = 0

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1
        if not self._results:
            raise AssertionError("No fake token remains.")

        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result

        return TokenIssueResponse(
            access_token=result,
            token_type="Bearer",
            expires_in=86_400,
        )


class CapturingTransport:
    def __init__(self, responses: Sequence[TossResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[TossRequest] = []

    async def send(self, request: TossRequest) -> TossResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("No fake response remains.")

        return self._responses.pop(0)


class ConcurrentRefreshTransport:
    def __init__(self, expected_old_requests: int) -> None:
        self.expected_old_requests = expected_old_requests
        self.old_requests = 0
        self.new_requests = 0
        self.all_old_requests_arrived = asyncio.Event()

    async def send(self, request: TossRequest) -> TossResponse:
        authorization = request.headers.get("Authorization")

        if authorization == "Bearer old-token":
            self.old_requests += 1
            if self.old_requests == self.expected_old_requests:
                self.all_old_requests_arrived.set()

            await self.all_old_requests_arrived.wait()
            return TossResponse(status_code=401)

        if authorization == "Bearer replacement-token":
            self.new_requests += 1
            return TossResponse(status_code=200)

        raise AssertionError("Unexpected Authorization header.")


@pytest.mark.asyncio
async def test_authenticated_client_adds_managed_header() -> None:
    issuer = SequenceIssuer(["secret-token"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport([TossResponse(status_code=200)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
        default_headers={"Accept": "application/json"},
    )
    caller_headers = {"X-Test": "yes"}

    response = await client.get(
        "/prices",
        headers=caller_headers,
    )

    assert response.status_code == 200
    assert issuer.call_count == 1
    assert caller_headers == {"X-Test": "yes"}
    assert transport.requests[0].headers == {
        "Accept": "application/json",
        "X-Test": "yes",
        "Authorization": "Bearer secret-token",
    }


@pytest.mark.asyncio
async def test_401_invalidates_token_and_retries_exactly_once() -> None:
    issuer = SequenceIssuer(["old-token", "replacement-token"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport(
        [
            TossResponse(status_code=401),
            TossResponse(status_code=200),
        ]
    )
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )

    response = await client.get("/prices")

    assert response.status_code == 200
    assert issuer.call_count == 2
    assert len(transport.requests) == 2
    assert transport.requests[0].headers["Authorization"] == ("Bearer old-token")
    assert transport.requests[1].headers["Authorization"] == (
        "Bearer replacement-token"
    )


@pytest.mark.asyncio
async def test_401_refresh_failure_fails_closed_without_second_request() -> None:
    issuer = SequenceIssuer(
        [
            "old-token",
            RuntimeError("ambiguous token timeout"),
        ]
    )
    manager = TossTokenManager(issuer)
    transport = CapturingTransport([TossResponse(status_code=401)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )

    with pytest.raises(
        TossTransportError,
        match="Token issuance failed unexpectedly",
    ):
        await client.get("/prices")

    assert issuer.call_count == 2
    assert len(transport.requests) == 1
    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_second_401_is_not_retried_and_token_is_invalidated() -> None:
    issuer = SequenceIssuer(["first-token", "second-token"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport(
        [
            TossResponse(status_code=401),
            TossResponse(status_code=401),
        ]
    )
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )

    with pytest.raises(TossAuthenticationError):
        await client.get("/prices")

    assert issuer.call_count == 2
    assert len(transport.requests) == 2
    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_non_401_error_is_not_retried() -> None:
    issuer = SequenceIssuer(["secret-token"])
    manager = TossTokenManager(issuer)
    transport = CapturingTransport([TossResponse(status_code=403)])
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )

    with pytest.raises(TossAuthorizationError):
        await client.get("/positions")

    assert issuer.call_count == 1
    assert len(transport.requests) == 1
    assert manager.cached_metadata() is not None


@pytest.mark.asyncio
async def test_concurrent_401_responses_share_one_replacement_token() -> None:
    request_count = 50
    issuer = SequenceIssuer(["old-token", "replacement-token"])
    manager = TossTokenManager(issuer)
    await manager.get_token()

    transport = ConcurrentRefreshTransport(request_count)
    client = TossHttpClient(
        "https://example.test",
        transport=transport,
        token_manager=manager,
    )

    responses = await asyncio.gather(
        *(client.get("/prices") for _ in range(request_count))
    )

    assert all(response.status_code == 200 for response in responses)
    assert issuer.call_count == 2
    assert transport.old_requests == request_count
    assert transport.new_requests == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_name",
    ["Authorization", "authorization", "AUTHORIZATION"],
)
async def test_managed_client_rejects_caller_authorization_header(
    header_name: str,
) -> None:
    manager = TossTokenManager(SequenceIssuer(["secret-token"]))
    client = TossHttpClient(
        "https://example.test",
        transport=CapturingTransport([TossResponse(status_code=200)]),
        token_manager=manager,
    )

    with pytest.raises(
        TossConfigurationError,
        match="managed by the token manager",
    ):
        await client.get(
            "/prices",
            headers={header_name: "Bearer caller-token"},
        )


@pytest.mark.parametrize(
    "header_name",
    ["Authorization", "authorization"],
)
def test_managed_client_rejects_default_authorization_header(
    header_name: str,
) -> None:
    manager = TossTokenManager(SequenceIssuer(["secret-token"]))

    with pytest.raises(
        TossConfigurationError,
        match="managed by the token manager",
    ):
        TossHttpClient(
            "https://example.test",
            token_manager=manager,
            default_headers={header_name: "Bearer caller-token"},
        )
