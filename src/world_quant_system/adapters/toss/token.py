from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True, repr=False)
class TokenIssueResponse:
    """Raw OAuth token fields returned by a token issuer.

    The access token is intentionally excluded from ``repr`` and ``str``.
    Validation is performed by :class:`TossTokenManager` so malformed
    responses are converted into adapter-specific exceptions.
    """

    access_token: str
    token_type: str
    expires_in: int

    def __repr__(self) -> str:
        return (
            "TokenIssueResponse("
            "access_token='********', "
            f"token_type={self.token_type!r}, "
            f"expires_in={self.expires_in!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Non-sensitive information about a cached access token."""

    token_type: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class AccessToken:
    """Validated access token with deliberately masked representations."""

    _secret: str
    token_type: str
    issued_at: datetime
    expires_at: datetime

    @property
    def metadata(self) -> TokenMetadata:
        return TokenMetadata(
            token_type=self.token_type,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
        )

    def authorization_header(self) -> str:
        """Return the sensitive Authorization header value.

        Callers must never log or persist the returned value.
        """

        return f"{self.token_type} {self._secret}"

    def __repr__(self) -> str:
        return (
            "AccessToken("
            "secret='********', "
            f"token_type={self.token_type!r}, "
            f"issued_at={self.issued_at!r}, "
            f"expires_at={self.expires_at!r})"
        )

    __str__ = __repr__
