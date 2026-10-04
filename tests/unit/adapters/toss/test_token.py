from datetime import UTC, datetime, timedelta

from world_quant_system.adapters.toss.token import (
    AccessToken,
    TokenIssueResponse,
)


def test_token_issue_response_masks_secret() -> None:
    response = TokenIssueResponse(
        access_token="super-secret-token",
        token_type="Bearer",
        expires_in=86_400,
    )

    rendered = repr(response)

    assert "super-secret-token" not in rendered
    assert "********" in rendered
    assert str(response) == rendered


def test_access_token_masks_secret_and_exposes_metadata() -> None:
    issued_at = datetime(2026, 7, 21, tzinfo=UTC)
    token = AccessToken(
        _secret="super-secret-token",
        token_type="Bearer",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=1),
    )

    rendered = repr(token)

    assert "super-secret-token" not in rendered
    assert "********" in rendered
    assert str(token) == rendered
    assert token.authorization_header() == "Bearer super-secret-token"
    assert token.metadata.token_type == "Bearer"
    assert token.metadata.issued_at == issued_at
    assert token.metadata.expires_at == issued_at + timedelta(days=1)
