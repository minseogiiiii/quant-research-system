from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.broker_certification.models import CertificationStatus
from world_quant_system.order_write_certification.compiler import (
    NoWriteDryRunTransport,
    TossOrderRequestCompiler,
)
from world_quant_system.order_write_certification.models import (
    OFFICIAL_TOSS_BASE_URL,
    DryRunAccountState,
    DryRunCheck,
    DryRunDecision,
    DryRunMarketState,
    HumanApprovalChallenge,
    OrderWriteDryRunReport,
    ReadOnlyCertificationEvidence,
    TossOrderCreateContract,
    TossOrderMarket,
    TossOrderWritePolicy,
    price_deviation_bps,
    validate_price_precision,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderType,
)

_ZERO = Decimal("0")


class DeterministicTossOrderWriteDryRunCertifier:
    """Certify the order-write boundary while keeping broker writes impossible."""

    def __init__(
        self,
        *,
        compiler: TossOrderRequestCompiler | None = None,
        transport: NoWriteDryRunTransport | None = None,
    ) -> None:
        self._compiler = compiler or TossOrderRequestCompiler()
        self._transport = transport or NoWriteDryRunTransport()

    def certify(
        self,
        *,
        intent: PaperOrderIntent,
        certification: ReadOnlyCertificationEvidence,
        account: DryRunAccountState,
        market: DryRunMarketState,
        policy: TossOrderWritePolicy,
        kill_switch: KillSwitchState,
        evaluated_at: datetime,
        contract: TossOrderCreateContract | None = None,
    ) -> OrderWriteDryRunReport:
        selected_contract = contract or TossOrderCreateContract()
        checks = self._evaluate_checks(
            intent=intent,
            certification=certification,
            account=account,
            market=market,
            policy=policy,
            kill_switch=kill_switch,
            evaluated_at=evaluated_at,
            contract=selected_contract,
        )
        failed_checks = tuple(check for check in checks if not check.passed)
        if failed_checks:
            decision = (
                DryRunDecision.INSUFFICIENT_EVIDENCE
                if any(
                    check.code.startswith("EVIDENCE_")
                    for check in failed_checks
                )
                else DryRunDecision.REJECTED
            )
            return OrderWriteDryRunReport(
                evaluated_at=evaluated_at,
                intent=intent,
                certification=certification,
                account=account,
                market=market,
                policy=policy,
                contract=selected_contract,
                kill_switch=kill_switch,
                checks=checks,
                decision=decision,
                compiled_request=None,
                approval_challenge=None,
                transport_receipt=None,
            )

        compiled_request = self._compiler.compile(
            intent=intent,
            policy=policy,
            compiled_at=evaluated_at,
            contract=selected_contract,
        )
        approval_challenge = HumanApprovalChallenge(
            request_digest=compiled_request.request_digest,
            account_fingerprint=policy.expected_account_fingerprint,
            created_at=evaluated_at,
            expires_at=evaluated_at
            + timedelta(seconds=policy.human_approval_ttl_seconds),
        )
        receipt = self._transport.execute(
            compiled_request,
            evaluated_at=evaluated_at,
        )
        return OrderWriteDryRunReport(
            evaluated_at=evaluated_at,
            intent=intent,
            certification=certification,
            account=account,
            market=market,
            policy=policy,
            contract=selected_contract,
            kill_switch=kill_switch,
            checks=checks,
            decision=DryRunDecision.HUMAN_APPROVAL_REQUIRED,
            compiled_request=compiled_request,
            approval_challenge=approval_challenge,
            transport_receipt=receipt,
        )

    def _evaluate_checks(
        self,
        *,
        intent: PaperOrderIntent,
        certification: ReadOnlyCertificationEvidence,
        account: DryRunAccountState,
        market: DryRunMarketState,
        policy: TossOrderWritePolicy,
        kill_switch: KillSwitchState,
        evaluated_at: datetime,
        contract: TossOrderCreateContract,
    ) -> tuple[DryRunCheck, ...]:
        checks: list[DryRunCheck] = []

        def add(code: str, passed: bool, message: str) -> None:
            checks.append(DryRunCheck(code=code, passed=passed, message=message))

        evaluated_at = evaluated_at.astimezone(UTC)
        add(
            "EVIDENCE_STATUS_PASS",
            certification.status is CertificationStatus.PASS,
            "Read-only adapter evidence must have PASS status.",
        )
        add(
            "EVIDENCE_BASE_URL_PINNED",
            certification.base_url == OFFICIAL_TOSS_BASE_URL,
            "Read-only evidence must use the official Toss base URL.",
        )
        add(
            "EVIDENCE_ACCOUNT_MATCH",
            certification.account_fingerprint
            == policy.expected_account_fingerprint,
            "Certification account fingerprint must match policy.",
        )
        add(
            "EVIDENCE_WRITE_DISABLED",
            not certification.write_operations_enabled,
            "Prior certification must keep write operations disabled.",
        )
        add(
            "EVIDENCE_AGE_VALID",
            _age_is_valid(
                certification.certified_at,
                evaluated_at,
                policy.maximum_certification_age_seconds,
            ),
            "Read-only certification evidence must be fresh and not future-dated.",
        )
        add(
            "CONTRACT_NETWORK_DISABLED",
            not contract.network_transport_enabled,
            "Pinned order contract cannot enable network transport.",
        )
        add(
            "KILL_SWITCH_NORMAL",
            kill_switch.mode is KillSwitchMode.NORMAL,
            "Kill switch must be NORMAL before request compilation.",
        )
        add(
            "ACCOUNT_FINGERPRINT_MATCH",
            account.account_fingerprint == policy.expected_account_fingerprint,
            "Account state fingerprint must match policy.",
        )
        add(
            "ACCOUNT_CURRENCY_MATCH",
            account.currency == policy.currency,
            "Account currency must match policy.",
        )
        add(
            "ACCOUNT_AGE_VALID",
            _age_is_valid(
                account.captured_at,
                evaluated_at,
                policy.maximum_account_age_seconds,
            ),
            "Account state must be fresh and not future-dated.",
        )
        add(
            "MARKET_SYMBOL_MATCH",
            market.symbol == intent.symbol,
            "Market symbol must match the order intent.",
        )
        add(
            "MARKET_CURRENCY_MATCH",
            market.currency == policy.currency,
            "Market currency must match policy.",
        )
        add(
            "MARKET_AGE_VALID",
            _age_is_valid(
                market.captured_at,
                evaluated_at,
                policy.maximum_market_age_seconds,
            ),
            "Market state must be fresh and not future-dated.",
        )
        add(
            "INTENT_MARKET_DIGEST_MATCH",
            intent.market_snapshot_digest == market.snapshot_digest,
            "Intent must bind to the exact market-state digest.",
        )
        add(
            "INTENT_NOT_FUTURE_DATED",
            intent.created_at.astimezone(UTC) <= evaluated_at,
            "Order intent cannot be created in the future.",
        )
        add(
            "LIMIT_ORDER_ONLY",
            intent.order_type is PaperOrderType.LIMIT,
            "Dry-run v1 permits limit orders only.",
        )
        add(
            "SYMBOL_ALLOWED",
            intent.symbol in policy.allowed_symbols,
            "Order symbol must be allowlisted.",
        )
        add(
            "SYMBOL_MARKET_FORMAT",
            _symbol_matches_market(intent.symbol, policy.market),
            "Order symbol must match the configured market format.",
        )
        add(
            "QUANTITY_WITHIN_LIMIT",
            intent.quantity <= policy.maximum_order_quantity,
            "Order quantity must not exceed the configured maximum.",
        )
        add(
            "NOTIONAL_WITHIN_LIMIT",
            intent.notional <= policy.maximum_order_notional,
            "Order notional must not exceed the configured maximum.",
        )
        add(
            "HIGH_VALUE_CONFIRMATION_NOT_REQUIRED",
            intent.notional < policy.high_value_order_threshold,
            "Dry-run v1 rejects orders requiring high-value confirmation.",
        )
        add(
            "OPEN_ORDER_LIMIT_AVAILABLE",
            account.open_order_count < policy.maximum_open_orders,
            "Account must remain below the open-order limit.",
        )
        add(
            "PRICE_PRECISION_VALID",
            validate_price_precision(
                price=intent.limit_price,
                market=policy.market,
            ),
            "Limit price must follow the pinned market precision rules.",
        )
        add(
            "PRICE_TICK_VALID",
            intent.limit_price % policy.price_tick == _ZERO,
            "Limit price must be a multiple of the configured price tick.",
        )
        lower_limit_valid = (
            market.lower_limit_price is None
            or intent.limit_price >= market.lower_limit_price
        )
        upper_limit_valid = (
            market.upper_limit_price is None
            or intent.limit_price <= market.upper_limit_price
        )
        add(
            "PRICE_BAND_VALID",
            lower_limit_valid and upper_limit_valid,
            "Limit price must stay inside the captured exchange price band.",
        )

        current_position = account.positions_by_symbol.get(intent.symbol)
        current_quantity = 0 if current_position is None else current_position.quantity
        if intent.side is PaperOrderSide.BUY:
            projected_quantity = current_quantity + intent.quantity
            reserve = (
                account.settled_cash
                * policy.minimum_cash_reserve_fraction
            )
            cash_valid = account.settled_cash - intent.notional >= reserve
            reference_price = market.ask
            marketable = intent.limit_price >= market.ask
        else:
            projected_quantity = current_quantity - intent.quantity
            cash_valid = True
            reference_price = market.bid
            marketable = intent.limit_price <= market.bid
        add(
            "NO_SHORT_POSITION",
            projected_quantity >= 0,
            "Sell orders cannot create a short position.",
        )
        add(
            "POSITION_WITHIN_LIMIT",
            0 <= projected_quantity <= policy.maximum_position_quantity,
            "Projected position must stay within the configured maximum.",
        )
        add(
            "CASH_RESERVE_VALID",
            cash_valid,
            "Buy orders must preserve the configured settled-cash reserve.",
        )
        add(
            "MARKETABLE_LIMIT_VALID",
            marketable or not policy.require_marketable_limit,
            "Limit order must be marketable when policy requires it.",
        )
        add(
            "LIMIT_DEVIATION_VALID",
            price_deviation_bps(intent.limit_price, reference_price)
            <= policy.maximum_limit_deviation_bps,
            "Limit price deviation must remain within policy.",
        )
        add(
            "CLIENT_ORDER_ID_CONTRACT_VALID",
            len(intent.client_order_id) <= contract.client_order_id_max_length,
            "clientOrderId must fit the official maximum length.",
        )
        return tuple(checks)


def _age_is_valid(
    observed_at: datetime,
    evaluated_at: datetime,
    maximum_age_seconds: int,
) -> bool:
    age = evaluated_at - observed_at.astimezone(UTC)
    return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)


def _symbol_matches_market(symbol: str, market: TossOrderMarket) -> bool:
    if market is TossOrderMarket.KR:
        return len(symbol) == 6 and symbol.isdigit()
    return bool(symbol) and symbol == symbol.upper() and symbol[0].isalpha()
