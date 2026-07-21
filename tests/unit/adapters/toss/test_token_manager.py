import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from world_quant_system.adapters.toss.errors import (
    TossAuthenticationError,
    TossConfigurationError,
    TossInvalidResponseError,
    TossTransportError,
)
from world_quant_system.adapters.toss.token import TokenIssueResponse
from world_quant_system.adapters.toss.token_manager import (
    TossTokenManager,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 21, tzinfo=UTC)
        self.monotonic_seconds = 10_000.0

    def now_utc(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.monotonic_seconds += seconds


class NaiveClock(FakeClock):
    def now_utc(self) -> datetime:
        return datetime(2026, 7, 21)


class SequenceIssuer:
    def __init__(
        self,
        results: Sequence[TokenIssueResponse | Exception],
    ) -> None:
        self._results = list(results)
        self.call_count = 0

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1

        if not self._results:
            raise AssertionError("No token result remains.")

        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result

        return result


class CountingIssuer:
    def __init__(self, *, expires_in: int = 86_400) -> None:
        self.expires_in = expires_in
        self.call_count = 0

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1
        return TokenIssueResponse(
            access_token=f"token-{self.call_count}",
            token_type="Bearer",
            expires_in=self.expires_in,
        )


class BlockingIssuer:
    def __init__(self) -> None:
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return TokenIssueResponse(
            access_token="shared-token",
            token_type="Bearer",
            expires_in=86_400,
        )


class AdvancingIssuer:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    async def issue_token(self) -> TokenIssueResponse:
        self.clock.advance(10)
        return TokenIssueResponse(
            access_token="delayed-token",
            token_type="Bearer",
            expires_in=60,
        )


def response(
    secret: str,
    *,
    token_type: str = "Bearer",
    expires_in: int = 86_400,
) -> TokenIssueResponse:
    return TokenIssueResponse(
        access_token=secret,
        token_type=token_type,
        expires_in=expires_in,
    )


@pytest.mark.asyncio
async def test_default_manager_blocks_token_issuance() -> None:
    manager = TossTokenManager()

    with pytest.raises(
        TossTransportError,
        match="Token issuer is not configured",
    ):
        await manager.get_token()


@pytest.mark.asyncio
async def test_manager_caches_token_before_refresh_boundary() -> None:
    clock = FakeClock()
    issuer = CountingIssuer()
    manager = TossTokenManager(issuer, clock=clock)

    first = await manager.get_token()
    clock.advance(86_099)
    second = await manager.get_token()

    assert first is second
    assert issuer.call_count == 1


@pytest.mark.asyncio
async def test_manager_refreshes_at_refresh_boundary() -> None:
    clock = FakeClock()
    issuer = CountingIssuer()
    manager = TossTokenManager(issuer, clock=clock)

    first = await manager.get_token()
    clock.advance(86_100)
    second = await manager.get_token()

    assert first is not second
    assert first.authorization_header() == "Bearer token-1"
    assert second.authorization_header() == "Bearer token-2"
    assert issuer.call_count == 2


@pytest.mark.asyncio
async def test_short_lifetime_uses_half_lifetime_as_maximum_skew() -> None:
    clock = FakeClock()
    issuer = CountingIssuer(expires_in=60)
    manager = TossTokenManager(
        issuer,
        clock=clock,
        refresh_skew=timedelta(minutes=5),
    )

    first = await manager.get_token()
    clock.advance(29)
    assert await manager.get_token() is first

    clock.advance(1)
    assert await manager.get_token() is not first
    assert issuer.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_issuance() -> None:
    issuer = BlockingIssuer()
    manager = TossTokenManager(issuer)

    tasks = [
        asyncio.create_task(manager.get_token())
        for _ in range(100)
    ]
    await issuer.started.wait()

    assert issuer.call_count == 1

    issuer.release.set()
    tokens = await asyncio.gather(*tasks)

    assert issuer.call_count == 1
    assert all(token is tokens[0] for token in tokens)


@pytest.mark.asyncio
async def test_invalidate_forces_next_call_to_refresh() -> None:
    issuer = CountingIssuer()
    manager = TossTokenManager(issuer)

    first = await manager.get_token()
    await manager.invalidate()
    second = await manager.get_token()

    assert first is not second
    assert issuer.call_count == 2


@pytest.mark.asyncio
async def test_force_refresh_replaces_cached_token() -> None:
    issuer = CountingIssuer()
    manager = TossTokenManager(issuer)

    first = await manager.get_token()
    second = await manager.force_refresh()

    assert first is not second
    assert issuer.call_count == 2


@pytest.mark.asyncio
async def test_cached_metadata_never_contains_secret() -> None:
    manager = TossTokenManager(
        SequenceIssuer([response("metadata-secret")])
    )

    assert manager.cached_metadata() is None
    await manager.get_token()
    metadata = manager.cached_metadata()

    assert metadata is not None
    assert "metadata-secret" not in repr(metadata)
    assert metadata.token_type == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_response",
    [
        response(""),
        response("   "),
        response("token", token_type="MAC"),
        response("token", expires_in=0),
        response("token", expires_in=-1),
    ],
)
async def test_manager_rejects_malformed_token_response(
    invalid_response: TokenIssueResponse,
) -> None:
    manager = TossTokenManager(
        SequenceIssuer([invalid_response])
    )

    with pytest.raises(TossInvalidResponseError):
        await manager.get_token()

    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_known_adapter_error_is_preserved() -> None:
    original = TossAuthenticationError(
        "Authentication failed.",
        status_code=401,
    )
    manager = TossTokenManager(SequenceIssuer([original]))

    with pytest.raises(TossAuthenticationError) as captured:
        await manager.get_token()

    assert captured.value is original
    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_unexpected_issuer_error_is_wrapped() -> None:
    manager = TossTokenManager(
        SequenceIssuer([RuntimeError("socket failure")])
    )

    with pytest.raises(
        TossTransportError,
        match="Token issuance failed unexpectedly",
    ) as captured:
        await manager.get_token()

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_failed_refresh_never_returns_possibly_invalid_old_token() -> None:
    clock = FakeClock()
    issuer = SequenceIssuer(
        [
            response("old-token"),
            RuntimeError("ambiguous timeout"),
            response("replacement-token"),
        ]
    )
    manager = TossTokenManager(issuer, clock=clock)

    old_token = await manager.get_token()
    clock.advance(86_100)

    with pytest.raises(TossTransportError):
        await manager.get_token()

    assert manager.cached_metadata() is None

    replacement = await manager.get_token()
    assert replacement is not old_token
    assert replacement.authorization_header() == (
        "Bearer replacement-token"
    )
    assert issuer.call_count == 3


@pytest.mark.asyncio
async def test_expiration_starts_after_issuance_completes() -> None:
    clock = FakeClock()
    manager = TossTokenManager(
        AdvancingIssuer(clock),
        clock=clock,
        refresh_skew=timedelta(0),
    )

    token = await manager.get_token()

    assert token.issued_at == datetime(
        2026,
        7,
        21,
        0,
        0,
        10,
        tzinfo=UTC,
    )
    assert token.expires_at == token.issued_at + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_manager_rejects_naive_clock() -> None:
    manager = TossTokenManager(
        SequenceIssuer([response("token")]),
        clock=NaiveClock(),
    )

    with pytest.raises(
        TossConfigurationError,
        match="timezone-aware",
    ):
        await manager.get_token()

    assert manager.cached_metadata() is None


def test_manager_rejects_negative_refresh_skew() -> None:
    with pytest.raises(
        TossConfigurationError,
        match="cannot be negative",
    ):
        TossTokenManager(
            refresh_skew=timedelta(seconds=-1)
        )


@pytest.mark.asyncio
async def test_seven_day_token_lifecycle_simulation() -> None:
    """Software backtest: poll every five minutes for seven days."""

    clock = FakeClock()
    issuer = CountingIssuer(expires_in=86_400)
    manager = TossTokenManager(
        issuer,
        clock=clock,
        refresh_skew=timedelta(minutes=5),
    )

    total_seconds = 7 * 24 * 60 * 60
    step_seconds = 5 * 60

    for _ in range((total_seconds // step_seconds) + 1):
        token = await manager.get_token()
        assert token.expires_at > clock.now_utc()
        clock.advance(step_seconds)

    # Refreshes occur at t=0 and every 23h55m thereafter.
    assert issuer.call_count == 8


class BlockingFailIssuer:
    def __init__(self) -> None:
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def issue_token(self) -> TokenIssueResponse:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        raise RuntimeError("shared failure")


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_failed_issuance() -> None:
    issuer = BlockingFailIssuer()
    manager = TossTokenManager(issuer)

    tasks = [
        asyncio.create_task(manager.get_token())
        for _ in range(100)
    ]
    await issuer.started.wait()
    issuer.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert issuer.call_count == 1
    assert all(isinstance(item, TossTransportError) for item in results)
    assert manager.cached_metadata() is None


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_issuance() -> None:
    issuer = BlockingIssuer()
    manager = TossTokenManager(issuer)

    cancelled_waiter = asyncio.create_task(manager.get_token())
    surviving_waiter = asyncio.create_task(manager.get_token())
    await issuer.started.wait()

    cancelled_waiter.cancel()
    issuer.release.set()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    token = await surviving_waiter

    assert token.authorization_header() == "Bearer shared-token"
    assert issuer.call_count == 1
    assert await manager.get_token() is token
