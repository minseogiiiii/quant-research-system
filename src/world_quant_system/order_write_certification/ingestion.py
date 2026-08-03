from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from world_quant_system.broker_certification.models import CertificationStatus
from world_quant_system.order_write_certification.models import (
    DryRunAccountState,
    DryRunMarketState,
    DryRunPosition,
    OrderWriteConfigurationError,
    OrderWriteIntegrityError,
    ReadOnlyCertificationEvidence,
    TossOrderMarket,
    TossOrderWritePolicy,
    decimal_from,
    parse_utc_datetime,
    sha256_document,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderType,
)


def load_order_intent(path: str | Path) -> PaperOrderIntent:
    document = _load_object(path)
    intent = PaperOrderIntent(
        decision_id=_text(document.get("decision_id"), "decision_id"),
        symbol=_text(document.get("symbol"), "symbol"),
        side=_parse_enum(
            PaperOrderSide,
            document.get("side"),
            "side",
        ),
        quantity=_integer(document.get("quantity"), "quantity"),
        limit_price=decimal_from(
            document.get("limit_price"),
            "limit_price",
        ),
        created_at=parse_utc_datetime(document.get("created_at"), "created_at"),
        market_snapshot_digest=_text(
            document.get("market_snapshot_digest"),
            "market_snapshot_digest",
        ),
        strategy_id=_text(document.get("strategy_id"), "strategy_id"),
        reason=_text(document.get("reason"), "reason"),
        order_type=_parse_enum(
            PaperOrderType,
            document.get("order_type", "limit"),
            "order_type",
        ),
    )
    _verify_optional_identity(document, "intent_id", intent.intent_id)
    _verify_optional_identity(
        document,
        "client_order_id",
        intent.client_order_id,
    )
    return intent


def load_read_only_certification(
    path: str | Path,
) -> ReadOnlyCertificationEvidence:
    document = _load_object(path)
    report_digest = _text(document.get("report_digest"), "report_digest")
    identity = dict(document)
    identity.pop("report_id", None)
    identity.pop("report_digest", None)
    computed_digest = sha256_document(identity)
    if computed_digest != report_digest:
        raise OrderWriteIntegrityError(
            "Read-only certification report digest does not match its content."
        )
    return ReadOnlyCertificationEvidence(
        provider=_text(document.get("provider"), "provider"),
        base_url=_text(document.get("base_url"), "base_url"),
        certified_at=parse_utc_datetime(
            document.get("certified_at"),
            "certified_at",
        ),
        account_fingerprint=_text(
            document.get("account_fingerprint"),
            "account_fingerprint",
        ),
        status=_parse_enum(
            CertificationStatus,
            document.get("status"),
            "status",
        ),
        report_digest=report_digest,
        network_transport_enabled=_boolean(
            document.get("network_transport_enabled"),
            "network_transport_enabled",
        ),
        write_operations_enabled=_boolean(
            document.get("write_operations_enabled"),
            "write_operations_enabled",
        ),
    )


def load_account_state(path: str | Path) -> DryRunAccountState:
    document = _load_object(path)
    positions_raw = document.get("positions", [])
    if not isinstance(positions_raw, list):
        raise OrderWriteConfigurationError("positions must be a list.")
    positions: list[DryRunPosition] = []
    for raw_position in positions_raw:
        if not isinstance(raw_position, dict):
            raise OrderWriteConfigurationError(
                "Each account position must be an object."
            )
        positions.append(
            DryRunPosition(
                symbol=_text(raw_position.get("symbol"), "position.symbol"),
                quantity=_integer(
                    raw_position.get("quantity"),
                    "position.quantity",
                ),
            )
        )
    state = DryRunAccountState(
        account_fingerprint=_text(
            document.get("account_fingerprint"),
            "account_fingerprint",
        ),
        currency=_text(document.get("currency"), "currency"),
        settled_cash=decimal_from(
            document.get("settled_cash"),
            "settled_cash",
        ),
        buying_power=decimal_from(
            document.get("buying_power"),
            "buying_power",
        ),
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
        open_order_count=_integer(
            document.get("open_order_count", 0),
            "open_order_count",
        ),
        captured_at=parse_utc_datetime(
            document.get("captured_at"),
            "captured_at",
        ),
        source_digest=_text(
            document.get("source_digest"),
            "source_digest",
        ),
    )
    _verify_optional_identity(
        document,
        "snapshot_digest",
        state.snapshot_digest,
    )
    return state


def load_market_state(path: str | Path) -> DryRunMarketState:
    document = _load_object(path)
    state = DryRunMarketState(
        symbol=_text(document.get("symbol"), "symbol"),
        bid=decimal_from(document.get("bid"), "bid"),
        ask=decimal_from(document.get("ask"), "ask"),
        last=decimal_from(document.get("last"), "last"),
        currency=_text(document.get("currency"), "currency"),
        captured_at=parse_utc_datetime(
            document.get("captured_at"),
            "captured_at",
        ),
        source_digest=_text(
            document.get("source_digest"),
            "source_digest",
        ),
        lower_limit_price=_optional_decimal(
            document.get("lower_limit_price"),
            "lower_limit_price",
        ),
        upper_limit_price=_optional_decimal(
            document.get("upper_limit_price"),
            "upper_limit_price",
        ),
    )
    _verify_optional_identity(
        document,
        "snapshot_digest",
        state.snapshot_digest,
    )
    return state


def load_policy(path: str | Path) -> TossOrderWritePolicy:
    document = _load_object(path)
    symbols_raw = document.get("allowed_symbols")
    if not isinstance(symbols_raw, list):
        raise OrderWriteConfigurationError("allowed_symbols must be a list.")
    allowed_symbols = tuple(
        sorted(_text(symbol, "allowed_symbols item") for symbol in symbols_raw)
    )
    return TossOrderWritePolicy(
        expected_account_fingerprint=_text(
            document.get("expected_account_fingerprint"),
            "expected_account_fingerprint",
        ),
        allowed_symbols=allowed_symbols,
        currency=_text(document.get("currency"), "currency"),
        market=_parse_enum(
            TossOrderMarket,
            document.get("market"),
            "market",
        ),
        maximum_market_age_seconds=_integer(
            document.get("maximum_market_age_seconds"),
            "maximum_market_age_seconds",
        ),
        maximum_account_age_seconds=_integer(
            document.get("maximum_account_age_seconds"),
            "maximum_account_age_seconds",
        ),
        maximum_certification_age_seconds=_integer(
            document.get("maximum_certification_age_seconds"),
            "maximum_certification_age_seconds",
        ),
        maximum_order_quantity=_integer(
            document.get("maximum_order_quantity"),
            "maximum_order_quantity",
        ),
        maximum_order_notional=decimal_from(
            document.get("maximum_order_notional"),
            "maximum_order_notional",
        ),
        maximum_position_quantity=_integer(
            document.get("maximum_position_quantity"),
            "maximum_position_quantity",
        ),
        maximum_open_orders=_integer(
            document.get("maximum_open_orders"),
            "maximum_open_orders",
        ),
        minimum_cash_reserve_fraction=decimal_from(
            document.get("minimum_cash_reserve_fraction"),
            "minimum_cash_reserve_fraction",
        ),
        maximum_limit_deviation_bps=decimal_from(
            document.get("maximum_limit_deviation_bps"),
            "maximum_limit_deviation_bps",
        ),
        price_tick=decimal_from(document.get("price_tick"), "price_tick"),
        human_approval_ttl_seconds=_integer(
            document.get("human_approval_ttl_seconds"),
            "human_approval_ttl_seconds",
        ),
        high_value_order_threshold=decimal_from(
            document.get("high_value_order_threshold"),
            "high_value_order_threshold",
        ),
        require_marketable_limit=_boolean(
            document.get("require_marketable_limit", True),
            "require_marketable_limit",
        ),
    )


def load_kill_switch(path: str | Path | None) -> KillSwitchState:
    if path is None:
        return KillSwitchState(
            mode=KillSwitchMode.NORMAL,
            reason="normal",
            activated_at=None,
        )
    document = _load_object(path)
    mode = _parse_enum(
        KillSwitchMode,
        document.get("mode"),
        "mode",
    )
    activated_raw = document.get("activated_at")
    activated_at = (
        None
        if activated_raw is None
        else parse_utc_datetime(activated_raw, "activated_at")
    )
    return KillSwitchState(
        mode=mode,
        reason=_text(document.get("reason", "normal"), "reason"),
        activated_at=activated_at,
        version=_integer(document.get("version", 0), "version"),
    )


def _load_object(path: str | Path) -> dict[str, object]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OrderWriteConfigurationError(
            f"Unable to load JSON document: {path}"
        ) from error
    if not isinstance(document, dict):
        raise OrderWriteConfigurationError("JSON document must be an object.")
    return cast(dict[str, object], document)


def _verify_optional_identity(
    document: dict[str, object],
    field_name: str,
    expected: str,
) -> None:
    value = document.get(field_name)
    if value is not None and value != expected:
        raise OrderWriteIntegrityError(
            f"{field_name} does not match the deterministic value."
        )


def _optional_decimal(
    value: object,
    field_name: str,
) -> Decimal | None:
    if value is None:
        return None
    return decimal_from(value, field_name)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderWriteConfigurationError(f"{field_name} must be text.")
    return value.strip()


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderWriteConfigurationError(f"{field_name} must be an integer.")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise OrderWriteConfigurationError(f"{field_name} must be boolean.")
    return value


def _parse_enum[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: object,
    field_name: str,
) -> EnumT:
    if not isinstance(value, str):
        raise OrderWriteConfigurationError(f"{field_name} must be text.")
    try:
        return enum_type(value)
    except ValueError as error:
        raise OrderWriteConfigurationError(
            f"{field_name} contains an unsupported value: {value}"
        ) from error
