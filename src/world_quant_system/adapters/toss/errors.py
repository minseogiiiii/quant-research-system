class TossAdapterError(Exception):
    """Base exception for every Toss adapter failure.

    Exception messages must not contain credentials, authorization
    headers, access tokens, account identifiers, or raw response bodies.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)

        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds

    def __str__(self) -> str:
        details: list[str] = []

        if self.status_code is not None:
            details.append(
                f"status_code={self.status_code}"
            )

        if self.request_id is not None:
            details.append(
                f"request_id={self.request_id}"
            )

        if self.retry_after_seconds is not None:
            details.append(
                "retry_after_seconds="
                f"{self.retry_after_seconds}"
            )

        if not details:
            return self.message

        return (
            f"{self.message} "
            f"({', '.join(details)})"
        )


class TossConfigurationError(TossAdapterError):
    """Raised when Toss adapter configuration is invalid."""


class TossTransportError(TossAdapterError):
    """Raised when a request cannot be delivered."""


class TossAuthenticationError(TossAdapterError):
    """Raised when authentication fails."""


class TossAuthorizationError(TossAdapterError):
    """Raised when Toss refuses access to a resource."""


class TossRateLimitError(TossAdapterError):
    """Raised when the request limit is exceeded."""


class TossClientResponseError(TossAdapterError):
    """Raised for non-authentication 4xx responses."""


class TossServerResponseError(TossAdapterError):
    """Raised for 5xx responses."""


class TossInvalidResponseError(TossAdapterError):
    """Raised for an invalid or unsupported response."""