from __future__ import annotations

from datetime import datetime

from world_quant_system.disabled_write_transport.models import (
    ApprovalScope,
    HumanApprovalGrant,
    TransportIntegrationCheck,
)
from world_quant_system.order_write_certification.models import (
    HumanApprovalChallenge,
)


def evaluate_approval_binding(
    challenge: HumanApprovalChallenge,
    grant: HumanApprovalGrant,
    *,
    evaluated_at: datetime,
) -> tuple[TransportIntegrationCheck, ...]:
    return (
        _check(
            "approval_challenge_binding",
            grant.challenge_id == challenge.approval_id,
            "Approval grant is bound to the dry-run challenge.",
            "Approval grant references a different challenge.",
        ),
        _check(
            "approval_request_binding",
            grant.request_digest == challenge.request_digest,
            "Approval grant is bound to the compiled request digest.",
            "Approval grant request digest does not match.",
        ),
        _check(
            "approval_account_binding",
            grant.account_fingerprint == challenge.account_fingerprint,
            "Approval grant is bound to the account fingerprint.",
            "Approval grant account fingerprint does not match.",
        ),
        _check(
            "approval_scope_single_create",
            grant.scope is ApprovalScope.ORDER_CREATE_ONCE,
            "Approval scope is limited to one order-create request.",
            "Approval scope is not limited to one order-create request.",
        ),
        _check(
            "approval_time_window",
            (
                challenge.created_at <= grant.granted_at <= evaluated_at
                and evaluated_at < grant.expires_at
                and grant.expires_at == challenge.expires_at
            ),
            "Approval grant and evaluation are within the challenge window.",
            "Approval grant is stale, premature, or outside the challenge window.",
        ),
    )


def _check(
    name: str,
    passed: bool,
    passed_detail: str,
    failed_detail: str,
) -> TransportIntegrationCheck:
    return TransportIntegrationCheck(
        name=name,
        passed=passed,
        detail=passed_detail if passed else failed_detail,
    )
