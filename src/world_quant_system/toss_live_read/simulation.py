from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from world_quant_system.credential_isolation.providers import FakeSecretProvider
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)
from world_quant_system.toss_live_read.models import (
    TossHttpResponse,
    TossLiveReadPolicy,
)
from world_quant_system.toss_live_read.service import (
    DeterministicTossLiveReadCertifier,
)


class FakeTransport:
    def __init__(self) -> None:
        self.call_count = 0
        self.broker_write_count = 0
        self.redirect_count = 0
        self._closed_page = 0

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
        common_headers = (
            ("x-ratelimit-limit", "10"),
            ("x-ratelimit-remaining", "9"),
        )
        if method == "POST" and path == "/oauth2/token":
            return TossHttpResponse(
                status_code=200,
                headers=common_headers,
                json_body={
                    "access_token": "fixture-live-token-not-real",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if method != "GET":
            raise AssertionError("Unexpected fake transport method.")
        if path == "/api/v1/accounts":
            document: object = {
                "result": [
                    {
                        "accountNo": "REDACTED-FIXTURE",
                        "accountSeq": 1,
                        "accountType": "BROKERAGE",
                    }
                ]
            }
        elif path == "/api/v1/holdings":
            document = {
                "result": {
                    "items": [
                        {
                            "symbol": "005930",
                            "currency": "KRW",
                            "quantity": "3",
                            "lastPrice": "95000",
                            "averagePurchasePrice": "90000",
                        }
                    ]
                }
            }
        elif path == "/api/v1/orders?status=OPEN":
            document = {
                "result": {
                    "orders": [],
                    "nextCursor": None,
                    "hasNext": False,
                }
            }
        elif path.startswith("/api/v1/orders?status=CLOSED"):
            self._closed_page += 1
            document = {
                "result": {
                    "orders": [],
                    "nextCursor": None,
                    "hasNext": False,
                }
            }
        else:
            raise AssertionError(f"Unexpected fake path: {path}")
        return TossHttpResponse(
            status_code=200,
            headers=common_headers,
            json_body=document,
        )


def main() -> None:
    now = datetime(2026, 8, 3, 23, 0, tzinfo=UTC)
    policy = TossLiveReadPolicy()
    transport = FakeTransport()
    material = FakeSecretProvider(
        client_id="fixture-client",
        client_secret="fixture-secret",
        account_id="1",
        credential_version="fixture-v1",
    ).load("toss")
    report = DeterministicTossLiveReadCertifier(
        policy=policy,
        transport=transport,
        sleeper=lambda _: None,
    ).certify(
        material=material,
        now=now,
        kill_switch=KillSwitchState(
            mode=KillSwitchMode.NORMAL,
            reason="normal",
            activated_at=None,
        ),
    )
    assert transport.broker_write_count == 0
    assert report.broker_write_count == 0
    assert material.destroyed
    print("Toss live read-only deterministic simulation passed.")
    print(f"Report ID: {report.report_id}")
    print(f"Report digest: {report.report_digest}")
    print("External network: SIMULATED")
    print("Broker writes: DISABLED")


if __name__ == "__main__":
    main()
