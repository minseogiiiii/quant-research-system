from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from world_quant_system.credential_isolation.providers import CredentialMaterial
from world_quant_system.paper_execution.models import KillSwitchMode, KillSwitchState
from world_quant_system.toss_live_read.http_transport import TossTransport
from world_quant_system.toss_live_read.models import (
    ACCOUNTS_PATH,
    HOLDINGS_PATH,
    ORDERS_PATH,
    CollectionEvidence,
    LiveReadCheck,
    LiveReadDecision,
    LiveReadOperation,
    RateLimitEvidence,
    TossHttpResponse,
    TossLiveReadCertificationReport,
    TossLiveReadIntegrityError,
    TossLiveReadPolicy,
    TossLiveReadSafetyError,
    TossLiveReadTransportError,
)
from world_quant_system.toss_live_read.oauth import TossOAuthClient
from world_quant_system.toss_live_read.parser import (
    build_orders_evidence,
    parse_account_evidence,
    parse_holdings_evidence,
    parse_order_page,
)


class DeterministicTossLiveReadCertifier:
    def __init__(
        self,
        *,
        policy: TossLiveReadPolicy,
        transport: TossTransport,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._sleeper = sleeper
        self._rate_headers: list[Mapping[str, str]] = []
        self._read_network_call_count = 0

    def certify(
        self,
        *,
        material: CredentialMaterial,
        now: datetime,
        kill_switch: KillSwitchState,
    ) -> TossLiveReadCertificationReport:
        self._rate_headers.clear()
        self._read_network_call_count = 0
        if self._transport.call_count != 0:
            raise TossLiveReadSafetyError(
                "Live-read transport must start with zero network calls."
            )
        if self._transport.broker_write_count != 0:
            raise TossLiveReadSafetyError(
                "Live-read transport must start with zero broker writes."
            )
        if self._transport.redirect_count != 0:
            raise TossLiveReadSafetyError(
                "Live-read transport must start with zero redirects."
            )
        try:
            return self._certify_with_material(
                material=material,
                now=now,
                kill_switch=kill_switch,
            )
        finally:
            if not material.destroyed:
                material.destroy()

    def _certify_with_material(
        self,
        *,
        material: CredentialMaterial,
        now: datetime,
        kill_switch: KillSwitchState,
    ) -> TossLiveReadCertificationReport:
        _require_aware(now)
        if kill_switch.mode is not KillSwitchMode.NORMAL:
            raise TossLiveReadSafetyError(
                "Live read-only certification requires a NORMAL kill switch."
            )
        with material.account_id.reveal_bytes() as account_bytes:
            try:
                account_seq = int(account_bytes.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as error:
                raise TossLiveReadSafetyError(
                    "WQS_TOSS_ACCOUNT_ID must contain the numeric accountSeq."
                ) from error
        if account_seq < 0:
            raise TossLiveReadSafetyError("accountSeq cannot be negative.")
        oauth = TossOAuthClient(self._transport)
        with oauth.issue(material=material, now=now) as token:
            with token.authorization_header() as authorization:
                accounts_response = self._read(
                    path=ACCOUNTS_PATH,
                    authorization=authorization,
                    account_seq=None,
                )
                account = parse_account_evidence(
                    accounts_response.json_body,
                    expected_account_seq=account_seq,
                    required_account_type=self._policy.required_account_type,
                )
                holdings_response = self._read(
                    path=HOLDINGS_PATH,
                    authorization=authorization,
                    account_seq=account_seq,
                )
                holdings = parse_holdings_evidence(holdings_response.json_body)
                open_orders = self._read_open_orders(
                    authorization=authorization,
                    account_seq=account_seq,
                )
                closed_orders = self._read_closed_orders(
                    authorization=authorization,
                    account_seq=account_seq,
                    now=now,
                )
            token_fingerprint = token.token_fingerprint
        auth_network_call_count = (
            self._transport.call_count - self._read_network_call_count
        )
        if auth_network_call_count != 1:
            raise TossLiveReadIntegrityError(
                "Certification must perform exactly one OAuth request."
            )
        broker_write_count = self._transport.broker_write_count
        if broker_write_count != 0:
            raise TossLiveReadSafetyError("Broker write count must remain zero.")
        if self._transport.redirect_count != 0:
            raise TossLiveReadSafetyError("Redirect count must remain zero.")
        checks = (
            LiveReadCheck("official_host", True, "Official Toss host pinned."),
            LiveReadCheck("oauth_single_issue", True, "One OAuth request used."),
            LiveReadCheck(
                "account_fingerprint",
                True,
                "Configured accountSeq matched exactly one BROKERAGE account.",
            ),
            LiveReadCheck(
                "read_only_allowlist",
                True,
                "Only accounts, holdings, and order-history GETs were used.",
            ),
            LiveReadCheck(
                "write_transport",
                True,
                "Broker write count remained zero.",
            ),
            LiveReadCheck(
                "secret_persistence",
                True,
                "Report contains fingerprints and digests only.",
            ),
        )
        return TossLiveReadCertificationReport(
            provider=self._policy.provider,
            decision=LiveReadDecision.CERTIFIED_READ_ONLY,
            generated_at=now.astimezone(UTC),
            policy=self._policy,
            account=account,
            holdings=holdings,
            open_orders=open_orders,
            closed_orders=closed_orders,
            rate_limits=_rate_limit_evidence(self._rate_headers),
            checks=checks,
            token_fingerprint=token_fingerprint,
            auth_network_call_count=auth_network_call_count,
            read_network_call_count=self._read_network_call_count,
        )

    def _read_open_orders(
        self,
        *,
        authorization: str,
        account_seq: int,
    ) -> CollectionEvidence:
        response = self._read(
            path=f"{ORDERS_PATH}?{urlencode({'status': 'OPEN'})}",
            authorization=authorization,
            account_seq=account_seq,
        )
        orders, cursor, has_next = parse_order_page(
            response.json_body,
            operation=LiveReadOperation.OPEN_ORDERS,
        )
        if has_next or cursor is not None:
            raise TossLiveReadIntegrityError(
                "OPEN orders must be returned without pagination."
            )
        return build_orders_evidence(
            operation=LiveReadOperation.OPEN_ORDERS,
            pages=[orders],
        )

    def _read_closed_orders(
        self,
        *,
        authorization: str,
        account_seq: int,
        now: datetime,
    ) -> CollectionEvidence:
        pages: list[list[dict[str, str]]] = []
        end_date = now.astimezone(ZoneInfo("Asia/Seoul")).date()
        start_date = end_date - timedelta(
            days=self._policy.closed_order_lookback_days - 1
        )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self._policy.maximum_closed_order_pages):
            query: dict[str, str | int] = {
                "status": "CLOSED",
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "limit": self._policy.closed_order_page_size,
            }
            if cursor is not None:
                query["cursor"] = cursor
            response = self._read(
                path=f"{ORDERS_PATH}?{urlencode(query)}",
                authorization=authorization,
                account_seq=account_seq,
            )
            orders, next_cursor, has_next = parse_order_page(
                response.json_body,
                operation=LiveReadOperation.CLOSED_ORDERS,
            )
            pages.append(orders)
            if not has_next:
                return build_orders_evidence(
                    operation=LiveReadOperation.CLOSED_ORDERS,
                    pages=pages,
                )
            assert next_cursor is not None
            if next_cursor in seen_cursors:
                raise TossLiveReadIntegrityError(
                    "Closed-order pagination cursor cycle detected."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise TossLiveReadIntegrityError(
            "Closed-order pagination exceeded the configured page limit."
        )

    def _read(
        self,
        *,
        path: str,
        authorization: str,
        account_seq: int | None,
    ) -> TossHttpResponse:
        headers = {
            "Accept": "application/json",
            "Authorization": authorization,
            "User-Agent": "world-quant-system/live-read-certification-v1",
        }
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        for attempt in range(1, self._policy.maximum_read_attempts + 1):
            self._read_network_call_count += 1
            response = self._transport.request(
                method="GET",
                path=path,
                headers=headers,
                body=None,
            )
            self._rate_headers.append(dict(response.headers))
            if response.status_code == 200:
                return response
            if response.status_code in {401, 403}:
                raise TossLiveReadSafetyError(
                    f"Read-only authorization failed with HTTP {response.status_code}."
                )
            retryable = response.status_code in {429, 500, 502, 503}
            if not retryable or attempt == self._policy.maximum_read_attempts:
                raise TossLiveReadTransportError(
                    f"Read-only request failed with HTTP {response.status_code}."
                )
            delay = _retry_delay(
                response,
                attempt=attempt,
                maximum=float(self._policy.maximum_retry_delay_seconds),
            )
            self._sleeper(delay)
        raise AssertionError("Unreachable read retry state.")


def _retry_delay(
    response: TossHttpResponse,
    *,
    attempt: int,
    maximum: float,
) -> float:
    for header_name in ("retry-after", "x-ratelimit-reset"):
        value = response.header(header_name)
        if value is not None:
            try:
                parsed = int(value)
            except ValueError:
                continue
            return min(maximum, max(0.0, float(parsed)))
    return min(maximum, float(2 ** (attempt - 1)))


def _rate_limit_evidence(
    observations: list[Mapping[str, str]],
) -> RateLimitEvidence:
    remaining: list[int] = []
    limits: list[int] = []
    retry_after: list[int] = []
    for headers in observations:
        _append_header_int(headers, "x-ratelimit-remaining", remaining)
        _append_header_int(headers, "x-ratelimit-limit", limits)
        _append_header_int(headers, "retry-after", retry_after)
    return RateLimitEvidence(
        observation_count=len(observations),
        minimum_remaining=min(remaining) if remaining else None,
        maximum_limit=max(limits) if limits else None,
        maximum_retry_after_seconds=max(retry_after) if retry_after else None,
    )


def _append_header_int(
    headers: Mapping[str, str],
    name: str,
    destination: list[int],
) -> None:
    value = headers.get(name)
    if value is None:
        return
    try:
        parsed = int(value)
    except ValueError:
        return
    if parsed >= 0:
        destination.append(parsed)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TossLiveReadSafetyError(
            "Certification timestamp must be timezone-aware."
        )
