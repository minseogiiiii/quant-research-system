from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urlencode

from world_quant_system.broker_certification.ingestion import ReadOnlyCaptureBundle
from world_quant_system.broker_certification.models import (
    AdapterCertificationReport,
    BrokerAdapterConfigurationError,
    BrokerAdapterIntegrityError,
    CertificationFinding,
    CertificationStatus,
    FindingSeverity,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedOrder,
    OperationEvidence,
    RateLimitObservation,
    ReadOnlyAdapterPolicy,
    ReadOnlyHttpMethod,
    ReadOnlyOperation,
    ReadOnlyRequest,
    ReadOnlyResponse,
)
from world_quant_system.broker_certification.parser import (
    TossReadOnlyResponseParser,
    extract_next_page_token,
)

_KNOWN_ORDER_STATUSES = frozenset(
    {
        "ACCEPTED",
        "CANCELED",
        "CANCELLED",
        "CLOSED",
        "FILLED",
        "OPEN",
        "PARTIALLY_FILLED",
        "PENDING",
        "REJECTED",
        "SUBMITTED",
        "WAITING",
    }
)


class DeterministicReadOnlyAdapterCertifier:
    """Certify captured GET responses without enabling broker transport."""

    def __init__(self, parser: TossReadOnlyResponseParser | None = None) -> None:
        self._parser = parser or TossReadOnlyResponseParser()

    def certify(
        self,
        *,
        policy: ReadOnlyAdapterPolicy,
        capture: ReadOnlyCaptureBundle,
        certified_at: datetime,
        bearer_token_placeholder: str = "fixture-token-not-a-secret",
    ) -> AdapterCertificationReport:
        if certified_at.tzinfo is None:
            raise BrokerAdapterConfigurationError(
                "Certification time must be timezone-aware."
            )
        certified_at = certified_at.astimezone(UTC)
        self._validate_capture_clock(policy, capture, certified_at)

        findings: list[CertificationFinding] = [
            CertificationFinding(
                code="NETWORK_TRANSPORT_DISABLED",
                severity=FindingSeverity.INFO,
                message=(
                    "Certification used captured responses; external network "
                    "transport remains disabled."
                ),
            ),
            CertificationFinding(
                code="WRITE_SURFACE_ABSENT",
                severity=FindingSeverity.INFO,
                message=(
                    "The certified surface contains GET operations only and "
                    "cannot submit, modify, or cancel orders."
                ),
            ),
        ]
        operations: list[OperationEvidence] = []
        accounts: list[NormalizedAccount] = []
        holdings: list[NormalizedHolding] = []
        orders: list[NormalizedOrder] = []

        for operation in policy.required_operations:
            responses = capture.responses_for(operation)
            if len(responses) > policy.maximum_pages_per_operation:
                findings.append(
                    CertificationFinding(
                        code="PAGE_LIMIT_EXCEEDED",
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"{operation.value} capture exceeds the configured "
                            "page limit."
                        ),
                        operation=operation,
                    )
                )
                responses = responses[: policy.maximum_pages_per_operation]

            request = _build_request(
                policy=policy,
                operation=operation,
                account_seq=capture.account_seq,
                bearer_token=bearer_token_placeholder,
            )
            evidence, operation_findings = self._inspect_responses(
                policy=policy,
                operation=operation,
                request=request,
                responses=responses,
            )
            findings.extend(operation_findings)
            operations.append(evidence)
            if operation is ReadOnlyOperation.ACCOUNTS:
                accounts.extend(self._parse_account_pages(responses, findings))
            elif operation is ReadOnlyOperation.HOLDINGS:
                holdings.extend(self._parse_holding_pages(responses, findings))
            else:
                orders.extend(self._parse_order_pages(responses, findings, operation))

        accounts_tuple = _deduplicate_accounts(accounts)
        holdings_tuple = _deduplicate_holdings(holdings)
        orders_tuple = _deduplicate_orders(orders)
        account_fingerprint = hashlib.sha256(capture.account_seq.encode()).hexdigest()
        self._check_account_identity(
            policy=policy,
            capture_account_seq=capture.account_seq,
            accounts=accounts_tuple,
            findings=findings,
        )
        self._check_order_statuses(orders_tuple, findings)
        status = _status_for(findings)
        return AdapterCertificationReport(
            provider=policy.provider,
            base_url=policy.base_url,
            certified_at=certified_at,
            policy_digest=policy.policy_digest,
            account_fingerprint=account_fingerprint,
            status=status,
            operations=tuple(operations),
            accounts=accounts_tuple,
            holdings=holdings_tuple,
            orders=orders_tuple,
            findings=tuple(findings),
            network_transport_enabled=False,
            write_operations_enabled=False,
        )

    def _inspect_responses(
        self,
        *,
        policy: ReadOnlyAdapterPolicy,
        operation: ReadOnlyOperation,
        request: ReadOnlyRequest,
        responses: Sequence[ReadOnlyResponse],
    ) -> tuple[OperationEvidence, tuple[CertificationFinding, ...]]:
        findings: list[CertificationFinding] = []
        response_digests: list[str] = []
        request_ids: list[str] = []
        rate_limits: list[RateLimitObservation] = []
        item_count = 0
        previous_page_token: str | None = None

        for index, response in enumerate(responses, start=1):
            if response.raw_size_bytes > policy.maximum_response_bytes:
                findings.append(
                    CertificationFinding(
                        code="RESPONSE_TOO_LARGE",
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"{operation.value} page {index} exceeds the "
                            "configured response-size limit."
                        ),
                        operation=operation,
                    )
                )
            if response.status_code != 200:
                findings.append(
                    CertificationFinding(
                        code="NON_SUCCESS_RESPONSE",
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"{operation.value} page {index} returned HTTP "
                            f"{response.status_code}."
                        ),
                        operation=operation,
                    )
                )
            headers = response.normalized_headers
            request_id = headers.get("x-request-id")
            if request_id is None or not request_id.strip():
                severity = (
                    FindingSeverity.CRITICAL
                    if policy.require_request_id
                    else FindingSeverity.WARNING
                )
                findings.append(
                    CertificationFinding(
                        code="REQUEST_ID_MISSING",
                        severity=severity,
                        message=(
                            f"{operation.value} page {index} has no "
                            "X-Request-Id header."
                        ),
                        operation=operation,
                    )
                )
                request_id = f"missing-{operation.value}-{index}"
            request_ids.append(request_id)
            rate_limit = RateLimitObservation.from_headers(headers)
            rate_limits.append(rate_limit)
            if rate_limit.limit is None or rate_limit.remaining is None:
                findings.append(
                    CertificationFinding(
                        code="RATE_LIMIT_HEADERS_INCOMPLETE",
                        severity=FindingSeverity.WARNING,
                        message=(
                            f"{operation.value} page {index} does not expose "
                            "complete rate-limit headers."
                        ),
                        operation=operation,
                    )
                )
            response_digests.append(response.response_digest)
            item_count += _count_items(operation, response.body)
            page_token = extract_next_page_token(response.body)
            if page_token is not None and page_token == previous_page_token:
                findings.append(
                    CertificationFinding(
                        code="PAGINATION_TOKEN_REPEATED",
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"{operation.value} repeated a pagination token."
                        ),
                        operation=operation,
                    )
                )
            previous_page_token = page_token

        return (
            OperationEvidence(
                operation=operation,
                request=request,
                response_digests=tuple(response_digests),
                page_count=len(responses),
                item_count=item_count,
                request_ids=tuple(request_ids),
                rate_limits=tuple(rate_limits),
            ),
            tuple(findings),
        )

    def _parse_account_pages(
        self,
        responses: Sequence[ReadOnlyResponse],
        findings: list[CertificationFinding],
    ) -> tuple[NormalizedAccount, ...]:
        return self._parse_pages(
            responses,
            parser=self._parser.parse_accounts,
            operation=ReadOnlyOperation.ACCOUNTS,
            findings=findings,
        )

    def _parse_holding_pages(
        self,
        responses: Sequence[ReadOnlyResponse],
        findings: list[CertificationFinding],
    ) -> tuple[NormalizedHolding, ...]:
        return self._parse_pages(
            responses,
            parser=self._parser.parse_holdings,
            operation=ReadOnlyOperation.HOLDINGS,
            findings=findings,
        )

    def _parse_order_pages(
        self,
        responses: Sequence[ReadOnlyResponse],
        findings: list[CertificationFinding],
        operation: ReadOnlyOperation,
    ) -> tuple[NormalizedOrder, ...]:
        return self._parse_pages(
            responses,
            parser=self._parser.parse_orders,
            operation=operation,
            findings=findings,
        )

    def _parse_pages[ItemT](
        self,
        responses: Sequence[ReadOnlyResponse],
        *,
        parser: Callable[[Mapping[str, object]], tuple[ItemT, ...]],
        operation: ReadOnlyOperation,
        findings: list[CertificationFinding],
    ) -> tuple[ItemT, ...]:
        parsed: list[ItemT] = []
        for index, response in enumerate(responses, start=1):
            if response.status_code != 200:
                continue
            try:
                parsed.extend(parser(response.body))
            except (
                BrokerAdapterConfigurationError,
                BrokerAdapterIntegrityError,
            ) as error:
                findings.append(
                    CertificationFinding(
                        code="RESPONSE_SCHEMA_REJECTED",
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"{operation.value} page {index} could not be "
                            f"normalized: {error}"
                        ),
                        operation=operation,
                    )
                )
        return tuple(parsed)

    def _check_account_identity(
        self,
        *,
        policy: ReadOnlyAdapterPolicy,
        capture_account_seq: str,
        accounts: tuple[NormalizedAccount, ...],
        findings: list[CertificationFinding],
    ) -> None:
        capture_fingerprint = hashlib.sha256(capture_account_seq.encode()).hexdigest()
        if capture_fingerprint != policy.expected_account_fingerprint:
            findings.append(
                CertificationFinding(
                    code="ACCOUNT_FINGERPRINT_MISMATCH",
                    severity=FindingSeverity.CRITICAL,
                    message=(
                        "Capture account fingerprint does not match the "
                        "configured allowlist."
                    ),
                    operation=ReadOnlyOperation.ACCOUNTS,
                )
            )
        matching_accounts = [
            account
            for account in accounts
            if account.account_fingerprint == policy.expected_account_fingerprint
        ]
        if len(matching_accounts) != 1:
            findings.append(
                CertificationFinding(
                    code="ACCOUNT_SELECTION_NOT_UNIQUE",
                    severity=FindingSeverity.CRITICAL,
                    message=(
                        "The expected account must appear exactly once in the "
                        "account-list response."
                    ),
                    operation=ReadOnlyOperation.ACCOUNTS,
                )
            )
        if len(accounts) > 1:
            findings.append(
                CertificationFinding(
                    code="MULTIPLE_ACCOUNTS_VISIBLE",
                    severity=FindingSeverity.WARNING,
                    message=(
                        "Multiple accounts are visible; downstream selection must "
                        "remain pinned to the configured fingerprint."
                    ),
                    operation=ReadOnlyOperation.ACCOUNTS,
                )
            )

    def _check_order_statuses(
        self,
        orders: tuple[NormalizedOrder, ...],
        findings: list[CertificationFinding],
    ) -> None:
        unknown_statuses = sorted(
            {order.status for order in orders} - _KNOWN_ORDER_STATUSES
        )
        for status in unknown_statuses:
            findings.append(
                CertificationFinding(
                    code="UNKNOWN_ORDER_STATUS",
                    severity=FindingSeverity.CRITICAL,
                    message=(
                        f"Order status {status!r} is not mapped; certification "
                        "must fail closed."
                    ),
                )
            )

    def _validate_capture_clock(
        self,
        policy: ReadOnlyAdapterPolicy,
        capture: ReadOnlyCaptureBundle,
        certified_at: datetime,
    ) -> None:
        capture_time = capture.captured_at.astimezone(UTC)
        if capture_time > certified_at:
            skew = (capture_time - certified_at).total_seconds()
            if skew > policy.allowed_clock_skew_seconds:
                raise BrokerAdapterIntegrityError(
                    "Capture timestamp is too far in the future."
                )


def _build_request(
    *,
    policy: ReadOnlyAdapterPolicy,
    operation: ReadOnlyOperation,
    account_seq: str,
    bearer_token: str,
) -> ReadOnlyRequest:
    endpoint = policy.endpoint_for(operation)
    query = urlencode(endpoint.fixed_query)
    url = f"{policy.base_url}{endpoint.path}"
    if query:
        url = f"{url}?{query}"
    headers: list[tuple[str, str]] = [
        ("Authorization", f"Bearer {bearer_token}"),
    ]
    if endpoint.account_header_required:
        headers.append(("X-Tossinvest-Account", account_seq))
    return ReadOnlyRequest(
        operation=operation,
        url=url,
        headers=tuple(headers),
        method=ReadOnlyHttpMethod.GET,
    )


def _status_for(findings: Iterable[CertificationFinding]) -> CertificationStatus:
    severities = {finding.severity for finding in findings}
    if FindingSeverity.CRITICAL in severities:
        return CertificationStatus.FAIL
    if FindingSeverity.WARNING in severities:
        return CertificationStatus.MANUAL_REVIEW_REQUIRED
    return CertificationStatus.PASS


def _count_items(
    operation: ReadOnlyOperation,
    body: Mapping[str, object],
) -> int:
    result = body.get("result")
    if isinstance(result, list):
        return len(result)
    if not isinstance(result, dict):
        return 0
    if operation is ReadOnlyOperation.HOLDINGS:
        keys: tuple[str, ...] = ("holdings", "items", "positions")
    elif operation in {
        ReadOnlyOperation.OPEN_ORDERS,
        ReadOnlyOperation.CLOSED_ORDERS,
    }:
        keys = ("orders", "items")
    else:
        return 1 if result else 0
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return 1 if result else 0


def _deduplicate_accounts(
    accounts: Sequence[NormalizedAccount],
) -> tuple[NormalizedAccount, ...]:
    return _deduplicate_by_key(
        accounts,
        key=lambda account: account.account_seq,
        document=lambda account: account.to_document(),
        label="account",
    )


def _deduplicate_holdings(
    holdings: Sequence[NormalizedHolding],
) -> tuple[NormalizedHolding, ...]:
    return _deduplicate_by_key(
        holdings,
        key=lambda holding: holding.symbol,
        document=lambda holding: holding.to_document(),
        label="holding",
    )


def _deduplicate_orders(
    orders: Sequence[NormalizedOrder],
) -> tuple[NormalizedOrder, ...]:
    return _deduplicate_by_key(
        orders,
        key=lambda order: order.order_id,
        document=lambda order: order.to_document(),
        label="order",
    )


def _deduplicate_by_key[ItemT](
    items: Sequence[ItemT],
    *,
    key: Callable[[ItemT], str],
    document: Callable[[ItemT], dict[str, object]],
    label: str,
) -> tuple[ItemT, ...]:
    selected: dict[str, ItemT] = {}
    documents: dict[str, dict[str, object]] = {}
    for item in items:
        item_key = key(item)
        item_document = document(item)
        if item_key in selected and documents[item_key] != item_document:
            raise BrokerAdapterIntegrityError(
                f"Conflicting duplicate {label}: {item_key}"
            )
        selected[item_key] = item
        documents[item_key] = item_document
    return tuple(selected[item_key] for item_key in sorted(selected))
