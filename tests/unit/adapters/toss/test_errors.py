from world_quant_system.adapters.toss.errors import (
    TossAuthenticationError,
    TossRateLimitError,
)


def test_error_string_contains_only_safe_metadata() -> None:
    error = TossAuthenticationError(
        "Authentication failed.",
        status_code=401,
        request_id="request-123",
    )

    message = str(error)

    assert message == (
        "Authentication failed. "
        "(status_code=401, request_id=request-123)"
    )


def test_error_without_metadata_uses_plain_message() -> None:
    error = TossAuthenticationError("Authentication failed.")

    assert str(error) == "Authentication failed."


def test_rate_limit_error_preserves_retry_delay() -> None:
    error = TossRateLimitError(
        "Rate limit exceeded.",
        status_code=429,
        request_id="rate-1",
        retry_after_seconds=7,
    )

    assert error.status_code == 429
    assert error.request_id == "rate-1"
    assert error.retry_after_seconds == 7
    assert "retry_after_seconds=7" in str(error)
