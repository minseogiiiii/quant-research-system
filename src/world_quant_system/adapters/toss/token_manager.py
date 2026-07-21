import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from world_quant_system.adapters.toss.errors import (
    TossAdapterError,
    TossConfigurationError,
    TossInvalidResponseError,
    TossTransportError,
)
from world_quant_system.adapters.toss.token import (
    AccessToken,
    TokenIssueResponse,
    TokenMetadata,
)


class TokenIssuer(Protocol):
    """Issue one OAuth access token without exposing credential details."""

    async def issue_token(self) -> TokenIssueResponse:
        """Return a raw token response."""
        ...


class Clock(Protocol):
    """Clock abstraction for deterministic token lifecycle tests."""

    def now_utc(self) -> datetime:
        """Return a timezone-aware UTC-compatible datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic timestamp in seconds."""
        ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class NoNetworkTokenIssuer:
    """Fail closed until a real OAuth issuer is explicitly configured."""

    async def issue_token(self) -> TokenIssueResponse:
        raise TossTransportError(
            "Token issuer is not configured."
        )


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: AccessToken
    refresh_at_monotonic: float
    expires_at_monotonic: float


class TossTokenManager:
    """Cache and refresh a single Toss OAuth access token safely.

    Toss documents that only one access token is valid per client and that
    issuing a new token immediately invalidates the previous token. This
    manager therefore uses a single-flight task so concurrent callers share
    one issuance attempt. It also fails closed after an ambiguous issuance
    failure instead of returning a possibly invalidated cached token.
    """

    def __init__(
        self,
        issuer: TokenIssuer | None = None,
        *,
        clock: Clock | None = None,
        refresh_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if refresh_skew < timedelta(0):
            raise TossConfigurationError(
                "Token refresh skew cannot be negative."
            )

        self._issuer = issuer or NoNetworkTokenIssuer()
        self._clock = clock or SystemClock()
        self._refresh_skew = refresh_skew
        self._state_lock = asyncio.Lock()
        self._cached: _CachedToken | None = None
        self._in_flight: asyncio.Task[AccessToken] | None = None

    async def get_token(self) -> AccessToken:
        """Return a cached token or share one refresh attempt."""

        cached = self._cached
        if cached is not None and self._is_fresh(cached):
            return cached.token

        async with self._state_lock:
            cached = self._cached
            if cached is not None and self._is_fresh(cached):
                return cached.token

            task = self._in_flight
            if task is not None and task.done():
                self._in_flight = None
                task = None

            if task is None:
                # A new issuance can invalidate the server-side old token.
                # Remove it before the attempt so failures never fall back
                # to a token whose validity is ambiguous.
                self._cached = None
                task = asyncio.create_task(self._issue_and_cache())
                self._in_flight = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._state_lock:
                    if self._in_flight is task:
                        self._in_flight = None

    async def force_refresh(self) -> AccessToken:
        """Discard the cached token and issue a replacement."""

        await self.invalidate()
        return await self.get_token()

    async def invalidate(self) -> None:
        """Remove the cached token without cancelling an active issuance."""

        async with self._state_lock:
            self._cached = None

    def cached_metadata(self) -> TokenMetadata | None:
        """Return non-sensitive cached-token metadata, if available."""

        cached = self._cached
        if cached is None:
            return None

        return cached.token.metadata

    async def _issue_and_cache(self) -> AccessToken:
        try:
            response = await self._issuer.issue_token()
        except TossAdapterError:
            raise
        except Exception as error:
            raise TossTransportError(
                "Token issuance failed unexpectedly."
            ) from error

        issued_at = self._validated_now_utc()
        issued_monotonic = self._clock.monotonic()
        token = self._build_token(response, issued_at)

        lifetime_seconds = float(response.expires_in)
        effective_skew_seconds = min(
            self._refresh_skew.total_seconds(),
            lifetime_seconds / 2,
        )
        expires_at_monotonic = issued_monotonic + lifetime_seconds
        cached = _CachedToken(
            token=token,
            refresh_at_monotonic=(
                expires_at_monotonic - effective_skew_seconds
            ),
            expires_at_monotonic=expires_at_monotonic,
        )

        async with self._state_lock:
            self._cached = cached

        return token

    def _build_token(
        self,
        response: TokenIssueResponse,
        issued_at: datetime,
    ) -> AccessToken:
        secret = response.access_token.strip()
        if not secret:
            raise TossInvalidResponseError(
                "Token response did not include an access token."
            )

        token_type = response.token_type.strip()
        if token_type.casefold() != "bearer":
            raise TossInvalidResponseError(
                "Token response used an unsupported token type."
            )

        if response.expires_in <= 0:
            raise TossInvalidResponseError(
                "Token response used a non-positive expiration."
            )

        return AccessToken(
            _secret=secret,
            token_type="Bearer",
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=response.expires_in),
        )

    def _is_fresh(self, cached: _CachedToken) -> bool:
        now = self._clock.monotonic()
        return (
            now < cached.refresh_at_monotonic
            and now < cached.expires_at_monotonic
        )

    def _validated_now_utc(self) -> datetime:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise TossConfigurationError(
                "Token clock must return a timezone-aware datetime."
            )

        return now.astimezone(UTC)
