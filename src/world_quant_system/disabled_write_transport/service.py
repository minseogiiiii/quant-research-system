from __future__ import annotations

from datetime import UTC, datetime, timedelta

from world_quant_system.credential_isolation.models import (
    CredentialPurpose,
    TokenLeaseMetadata,
    TokenLeaseState,
    TokenUseReceipt,
)
from world_quant_system.disabled_write_transport.approval import (
    evaluate_approval_binding,
)
from world_quant_system.disabled_write_transport.endpoint_policy import (
    PinnedTossWriteEndpointPolicy,
)
from world_quant_system.disabled_write_transport.models import (
    AuthorizedWriteEnvelope,
    DisabledWriteTransportConfigurationError,
    DisabledWriteTransportPolicy,
    DisabledWriteTransportReport,
    HumanApprovalGrant,
    IntegrationDecision,
    TransportIntegrationCheck,
)
from world_quant_system.disabled_write_transport.transport import (
    DisabledTossWriteTransport,
    TossWriteTransport,
)
from world_quant_system.order_write_certification.models import (
    DryRunDecision,
    OrderWriteDryRunReport,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


class DeterministicDisabledWriteTransportIntegrator:
    def __init__(
        self,
        *,
        endpoint_policy: PinnedTossWriteEndpointPolicy | None = None,
        transport: TossWriteTransport | None = None,
    ) -> None:
        self._endpoint_policy = endpoint_policy or PinnedTossWriteEndpointPolicy()
        self._transport = transport or DisabledTossWriteTransport()

    async def certify(
        self,
        *,
        dry_run_report: OrderWriteDryRunReport,
        approval_grant: HumanApprovalGrant,
        token_lease: TokenLeaseMetadata,
        token_use: TokenUseReceipt,
        kill_switch: KillSwitchState,
        policy: DisabledWriteTransportPolicy,
        evaluated_at: datetime,
    ) -> DisabledWriteTransportReport:
        normalized_at = _normalized_utc(evaluated_at)
        challenge = dry_run_report.approval_challenge
        compiled_request = dry_run_report.compiled_request
        if challenge is None or compiled_request is None:
            raise DisabledWriteTransportConfigurationError(
                "Dry-run report does not contain approval and request evidence."
            )

        checks: list[TransportIntegrationCheck] = [
            _check(
                "dry_run_report_approval_required",
                dry_run_report.decision
                is DryRunDecision.HUMAN_APPROVAL_REQUIRED,
                "Dry-run report reached the human-approval boundary.",
                "Dry-run report did not reach the human-approval boundary.",
            ),
            _check(
                "compiled_request_identity",
                compiled_request.request_digest == challenge.request_digest,
                "Compiled request digest matches the approval challenge.",
                "Compiled request digest does not match the approval challenge.",
            ),
            _check(
                "compiled_request_not_sendable",
                not compiled_request.network_sendable
                and not compiled_request.credentials_loaded,
                "Compiled request has no network or credential capability.",
                "Compiled request unexpectedly enables network or credentials.",
            ),
        ]
        checks.extend(
            self._endpoint_policy.evaluate(compiled_request, policy)
        )
        checks.extend(
            evaluate_approval_binding(
                challenge,
                approval_grant,
                evaluated_at=normalized_at,
            )
        )
        checks.extend(
            _credential_checks(
                token_lease=token_lease,
                token_use=token_use,
                request_account_fingerprint=(
                    compiled_request.account_fingerprint
                ),
                evaluated_at=normalized_at,
            )
        )
        checks.extend(
            (
                _check(
                    "kill_switch_normal",
                    kill_switch.mode is KillSwitchMode.NORMAL,
                    "Kill switch is NORMAL.",
                    "Kill switch blocks write-path integration.",
                ),
                _check(
                    "network_and_write_flags_disabled",
                    (
                        not policy.external_network_enabled
                        and not policy.broker_writes_enabled
                        and not policy.submission_states_enabled
                        and not policy.automatic_retries_enabled
                        and not policy.allow_redirects
                    ),
                    (
                        "Network, writes, retries, redirects, and submission "
                        "states are disabled."
                    ),
                    "One or more forbidden transport capabilities are enabled.",
                ),
            )
        )

        if any(not check.passed for check in checks):
            return DisabledWriteTransportReport(
                evaluated_at=normalized_at,
                policy=policy,
                dry_run_report_digest=dry_run_report.report_digest,
                challenge=challenge,
                checks=tuple(checks),
                decision=IntegrationDecision.REJECTED,
                envelope=None,
                receipt=None,
            )

        envelope_expires_at = min(
            approval_grant.expires_at,
            token_lease.expires_at,
            normalized_at
            + timedelta(seconds=policy.maximum_envelope_ttl_seconds),
        )
        envelope = AuthorizedWriteEnvelope(
            compiled_request=compiled_request,
            approval_grant=approval_grant,
            token_lease=token_lease,
            token_use=token_use,
            created_at=normalized_at,
            expires_at=envelope_expires_at,
        )
        receipt = await self._transport.submit(
            envelope,
            evaluated_at=normalized_at,
        )
        checks.append(
            _check(
                "terminal_transport_block",
                (
                    receipt.network_call_count == 0
                    and receipt.broker_write_count == 0
                    and receipt.redirect_count == 0
                ),
                "Terminal transport blocked without network or broker writes.",
                "Terminal transport recorded a forbidden side effect.",
            )
        )
        return DisabledWriteTransportReport(
            evaluated_at=normalized_at,
            policy=policy,
            dry_run_report_digest=dry_run_report.report_digest,
            challenge=challenge,
            checks=tuple(checks),
            decision=IntegrationDecision.CERTIFIED_BLOCKED,
            envelope=envelope,
            receipt=receipt,
        )


def _credential_checks(
    *,
    token_lease: TokenLeaseMetadata,
    token_use: TokenUseReceipt,
    request_account_fingerprint: str,
    evaluated_at: datetime,
) -> tuple[TransportIntegrationCheck, ...]:
    state_usable = token_lease.state in {
        TokenLeaseState.ACTIVE,
        TokenLeaseState.EXPIRING,
    }
    return (
        _check(
            "credential_purpose_binding",
            (
                token_lease.purpose is CredentialPurpose.ORDER_WRITE_DRY_RUN
                and token_use.purpose is CredentialPurpose.ORDER_WRITE_DRY_RUN
            ),
            "Credential lease and use receipt are purpose-bound to dry-run writes.",
            "Credential purpose is not valid for this write path.",
        ),
        _check(
            "credential_account_binding",
            token_lease.account_fingerprint
            == request_account_fingerprint,
            "Credential lease matches the request account fingerprint.",
            "Credential lease account fingerprint does not match.",
        ),
        _check(
            "credential_receipt_binding",
            (
                token_use.lease_id == token_lease.lease_id
                and token_use.token_fingerprint
                == token_lease.token_fingerprint
                and token_use.authorization_header == "Bearer [REDACTED]"
                and token_lease.use_count >= 1
            ),
            "Token-use receipt matches the active lease and remains redacted.",
            "Token-use receipt does not match the lease or exposes unsafe data.",
        ),
        _check(
            "credential_time_window",
            (
                state_usable
                and token_lease.issued_at <= token_use.used_at <= evaluated_at
                and evaluated_at < token_lease.expires_at
            ),
            "Token lease is active and within its time window.",
            "Token lease is inactive, expired, or used outside its time window.",
        ),
        _check(
            "credential_side_effect_counts",
            (
                token_use.network_call_count == 0
                and token_use.broker_write_count == 0
            ),
            "Credential authorization recorded no network or broker writes.",
            "Credential authorization recorded a forbidden side effect.",
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


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DisabledWriteTransportConfigurationError(
            "Transport evaluation time must be timezone-aware."
        )
    return value.astimezone(UTC)
