from __future__ import annotations

from datetime import datetime

from world_quant_system.order_write_certification.models import (
    CompiledTossOrderRequest,
    DryRunTransportReceipt,
    TossOrderCreateContract,
    TossOrderWritePolicy,
    decimal_text,
)
from world_quant_system.paper_execution.models import PaperOrderIntent


class TossOrderRequestCompiler:
    """Compile a non-sendable Toss order request with redacted headers."""

    def compile(
        self,
        *,
        intent: PaperOrderIntent,
        policy: TossOrderWritePolicy,
        compiled_at: datetime,
        contract: TossOrderCreateContract | None = None,
    ) -> CompiledTossOrderRequest:
        selected_contract = contract or TossOrderCreateContract()
        body: dict[str, object] = {
            "clientOrderId": intent.client_order_id,
            "symbol": intent.symbol,
            "side": intent.side.value.upper(),
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": str(intent.quantity),
            "price": decimal_text(intent.limit_price),
            "confirmHighValueOrder": False,
        }
        headers = (
            ("Authorization", "Bearer [REDACTED]"),
            ("Content-Type", "application/json"),
            (
                "X-Tossinvest-Account",
                f"sha256:{policy.expected_account_fingerprint}",
            ),
        )
        return CompiledTossOrderRequest(
            contract=selected_contract,
            account_fingerprint=policy.expected_account_fingerprint,
            headers=tuple(sorted(headers)),
            body=tuple(sorted(body.items())),
            compiled_at=compiled_at,
            network_sendable=False,
            credentials_loaded=False,
        )


class NoWriteDryRunTransport:
    """Return evidence without importing or invoking a network transport."""

    def execute(
        self,
        request: CompiledTossOrderRequest,
        *,
        evaluated_at: datetime,
    ) -> DryRunTransportReceipt:
        return DryRunTransportReceipt(
            request_digest=request.request_digest,
            evaluated_at=evaluated_at,
            broker_write_count=0,
            network_call_count=0,
        )
