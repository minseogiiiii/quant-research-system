from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from world_quant_system.broker_certification.models import (
    BrokerAdapterIntegrityError,
    BrokerAdapterSafetyError,
    ReadOnlyOperation,
    ReadOnlyRequest,
    ReadOnlyResponse,
)


class ReadOnlyTransport(Protocol):
    """Narrow transport surface for already-built GET requests."""

    async def send(self, request: ReadOnlyRequest) -> ReadOnlyResponse:
        """Return one response without mutating broker state."""
        ...


class NoNetworkReadOnlyTransport:
    """Default transport that always refuses external connectivity."""

    async def send(self, request: ReadOnlyRequest) -> ReadOnlyResponse:
        del request
        raise BrokerAdapterSafetyError(
            "Network transport is disabled for adapter-certification v1."
        )


class FixtureReadOnlyTransport:
    """Deterministic response transport used by certification tests."""

    def __init__(
        self,
        responses: Mapping[ReadOnlyOperation, Sequence[ReadOnlyResponse]],
    ) -> None:
        self._responses = {
            operation: deque(operation_responses)
            for operation, operation_responses in responses.items()
        }
        self._calls: list[dict[str, object]] = []

    @property
    def calls(self) -> tuple[dict[str, object], ...]:
        return tuple(self._calls)

    async def send(self, request: ReadOnlyRequest) -> ReadOnlyResponse:
        self._calls.append(request.safe_document)
        try:
            queue = self._responses[request.operation]
        except KeyError as error:
            raise BrokerAdapterIntegrityError(
                f"No fixture responses configured for {request.operation.value}."
            ) from error
        if not queue:
            raise BrokerAdapterIntegrityError(
                f"Fixture responses exhausted for {request.operation.value}."
            )
        return queue.popleft()


class RetryingReadOnlyClient:
    """Bounded retry wrapper for safe GET requests only."""

    def __init__(
        self,
        transport: ReadOnlyTransport,
        *,
        maximum_attempts: int,
        base_backoff_seconds: float,
        maximum_backoff_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if base_backoff_seconds < 0 or maximum_backoff_seconds < 0:
            raise ValueError("backoff values must be nonnegative")
        if maximum_backoff_seconds < base_backoff_seconds:
            raise ValueError("maximum backoff cannot be below base backoff")
        self._transport = transport
        self._maximum_attempts = maximum_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._maximum_backoff_seconds = maximum_backoff_seconds
        self._sleep = sleep

    async def get(self, request: ReadOnlyRequest) -> ReadOnlyResponse:
        last_response: ReadOnlyResponse | None = None
        for attempt in range(1, self._maximum_attempts + 1):
            response = await self._transport.send(request)
            last_response = response
            if not _is_retryable(response):
                return response
            if attempt == self._maximum_attempts:
                break
            await self._sleep(self._delay_for(response, attempt))
        assert last_response is not None
        return last_response

    def _delay_for(self, response: ReadOnlyResponse, attempt: int) -> float:
        retry_after = response.normalized_headers.get("retry-after")
        if retry_after is not None:
            try:
                parsed_retry_after = float(retry_after)
            except ValueError:
                parsed_retry_after = 0.0
            if parsed_retry_after >= 0:
                return min(parsed_retry_after, self._maximum_backoff_seconds)
        exponential = self._base_backoff_seconds * (2 ** (attempt - 1))
        deterministic_jitter = min(0.05 * attempt, 0.25)
        return float(
            min(
                exponential + deterministic_jitter,
                self._maximum_backoff_seconds,
            )
        )


def _is_retryable(response: ReadOnlyResponse) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code >= 500:
        return True
    error = response.body.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    return code in {"internal-error", "maintenance", "rate-limit-exceeded"}


def group_fixture_responses(
    entries: Sequence[tuple[ReadOnlyOperation, ReadOnlyResponse]],
) -> dict[ReadOnlyOperation, tuple[ReadOnlyResponse, ...]]:
    grouped: dict[ReadOnlyOperation, list[ReadOnlyResponse]] = defaultdict(list)
    for operation, response in entries:
        grouped[operation].append(response)
    return {
        operation: tuple(operation_responses)
        for operation, operation_responses in grouped.items()
    }
