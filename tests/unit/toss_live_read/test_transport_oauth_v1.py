from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from world_quant_system.credential_isolation.providers import FakeSecretProvider
from world_quant_system.toss_live_read.http_transport import _validate_request_boundary
from world_quant_system.toss_live_read.models import (
    TossHttpResponse,
    TossLiveReadSafetyError,
)
from world_quant_system.toss_live_read.oauth import TossOAuthClient


class OAuthFixtureTransport:
    def __init__(self) -> None:
        self.call_count = 0
        self.broker_write_count = 0
        self.redirect_count = 0
        self.captured_body = b""

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> TossHttpResponse:
        del headers
        self.call_count += 1
        assert method == "POST"
        assert path == "/oauth2/token"
        assert body is not None
        self.captured_body = body
        return TossHttpResponse(
            status_code=200,
            headers=(),
            json_body={
                "access_token": "secret-token-value",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )


def test_transport_boundary_blocks_order_post() -> None:
    with pytest.raises(TossLiveReadSafetyError):
        _validate_request_boundary("POST", "/api/v1/orders")


def test_oauth_token_is_ephemeral_and_fingerprinted() -> None:
    material = FakeSecretProvider(
        client_id="client-id",
        client_secret="client-secret",
        account_id="1",
        credential_version="v1",
    ).load("toss")
    transport = OAuthFixtureTransport()
    token = TossOAuthClient(transport).issue(
        material=material,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )
    destroyed_before = token.destroyed
    assert destroyed_before is False
    assert "client-secret" in transport.captured_body.decode()
    with token.authorization_header() as header:
        assert header == "Bearer secret-token-value"
    token.destroy()
    assert token.destroyed is True
    assert token.secret_destroyed is True
