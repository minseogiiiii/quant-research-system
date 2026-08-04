from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from world_quant_system.credential_isolation.providers import (
    CredentialMaterial,
    FakeSecretProvider,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)
from world_quant_system.toss_live_read.models import (
    TossHttpResponse,
    TossLiveReadIntegrityError,
    TossLiveReadPolicy,
    TossLiveReadSafetyError,
)
from world_quant_system.toss_live_read.service import (
    DeterministicTossLiveReadCertifier,
)


class ScenarioTransport:
    def __init__(self, *, cycle: bool = False) -> None:
        self.call_count = 0
        self.broker_write_count = 0
        self.redirect_count = 0
        self.cycle = cycle

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> TossHttpResponse:
        del headers, body
        self.call_count += 1
        if method == "POST":
            return response(
                {
                    "access_token": "test-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )
        if path == "/api/v1/accounts":
            return response(
                {
                    "result": [
                        {
                            "accountNo": "never-persist-this",
                            "accountSeq": 1,
                            "accountType": "BROKERAGE",
                        }
                    ]
                }
            )
        if path == "/api/v1/holdings":
            return response({"result": {"items": []}})
        if path == "/api/v1/orders?status=OPEN":
            return response(
                {"result": {"orders": [], "nextCursor": None, "hasNext": False}}
            )
        if "status=CLOSED" in path:
            if self.cycle:
                return response(
                    {
                        "result": {
                            "orders": [],
                            "nextCursor": "same-cursor",
                            "hasNext": True,
                        }
                    }
                )
            return response(
                {"result": {"orders": [], "nextCursor": None, "hasNext": False}}
            )
        raise AssertionError(path)


def response(document: object) -> TossHttpResponse:
    return TossHttpResponse(
        status_code=200,
        headers=(("x-ratelimit-remaining", "8"),),
        json_body=document,
    )


def material() -> CredentialMaterial:
    return FakeSecretProvider(
        client_id="client",
        client_secret="secret",
        account_id="1",
        credential_version="v1",
    ).load("toss")


def normal_switch(now: datetime) -> KillSwitchState:
    return KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )


def test_service_certifies_read_only_and_destroys_material() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    secret_material = material()
    report = DeterministicTossLiveReadCertifier(
        policy=TossLiveReadPolicy(),
        transport=ScenarioTransport(),
        sleeper=lambda _: None,
    ).certify(
        material=secret_material,
        now=now,
        kill_switch=normal_switch(now),
    )
    assert report.broker_write_count == 0
    assert report.order_submission_enabled is False
    assert report.auth_network_call_count == 1
    assert report.read_network_call_count == 4
    assert secret_material.destroyed
    serialized = str(report.to_document())
    assert "never-persist-this" not in serialized
    assert "test-token" not in serialized
    assert "fixture-secret" not in serialized
    assert "client-secret" not in serialized


def test_service_rejects_non_normal_kill_switch_before_network() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    transport = ScenarioTransport()
    with pytest.raises(TossLiveReadSafetyError, match="NORMAL"):
        DeterministicTossLiveReadCertifier(
            policy=TossLiveReadPolicy(),
            transport=transport,
        ).certify(
            material=material(),
            now=now,
            kill_switch=KillSwitchState(
                mode=KillSwitchMode.HARD_HALT,
                reason="test halt",
                activated_at=now,
            ),
        )
    assert transport.call_count == 0


def test_service_rejects_closed_order_cursor_cycle() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    with pytest.raises(TossLiveReadIntegrityError, match="cycle"):
        DeterministicTossLiveReadCertifier(
            policy=TossLiveReadPolicy(),
            transport=ScenarioTransport(cycle=True),
            sleeper=lambda _: None,
        ).certify(
            material=material(),
            now=now,
            kill_switch=normal_switch(now),
        )


def test_service_destroys_material_when_account_check_fails() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    secret_material = FakeSecretProvider(
        client_id="client",
        client_secret="secret",
        account_id="99",
        credential_version="v1",
    ).load("toss")
    with pytest.raises(TossLiveReadIntegrityError, match="exactly one"):
        DeterministicTossLiveReadCertifier(
            policy=TossLiveReadPolicy(),
            transport=ScenarioTransport(),
            sleeper=lambda _: None,
        ).certify(
            material=secret_material,
            now=now,
            kill_switch=normal_switch(now),
        )
    assert secret_material.destroyed


class RetryScenarioTransport(ScenarioTransport):
    def __init__(self, *, status_code: int) -> None:
        super().__init__()
        self._status_code = status_code
        self._failed = False

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> TossHttpResponse:
        if method == "GET" and path == "/api/v1/holdings" and not self._failed:
            self._failed = True
            self.call_count += 1
            return TossHttpResponse(
                status_code=self._status_code,
                headers=(("retry-after", "2"),),
                json_body={"error": {"code": "temporary"}},
            )
        return super().request(
            method=method,
            path=path,
            headers=headers,
            body=body,
        )


def test_read_429_uses_bounded_retry() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    delays: list[float] = []
    report = DeterministicTossLiveReadCertifier(
        policy=TossLiveReadPolicy(),
        transport=RetryScenarioTransport(status_code=429),
        sleeper=delays.append,
    ).certify(
        material=material(),
        now=now,
        kill_switch=normal_switch(now),
    )
    assert delays == [2.0]
    assert report.read_network_call_count == 5


def test_read_401_is_not_retried() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    transport = RetryScenarioTransport(status_code=401)
    with pytest.raises(TossLiveReadSafetyError, match="401"):
        DeterministicTossLiveReadCertifier(
            policy=TossLiveReadPolicy(),
            transport=transport,
            sleeper=lambda _: None,
        ).certify(
            material=material(),
            now=now,
            kill_switch=normal_switch(now),
        )
    assert transport.call_count == 3
