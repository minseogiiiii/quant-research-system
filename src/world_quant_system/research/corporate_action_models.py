from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.research.models import (
    ResearchConfigurationError,
    ResearchConflictError,
    ResearchError,
    ResearchIntegrityError,
    ResearchNotFoundError,
)

_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTION_NAMESPACE = UUID("e4c08622-38a4-5f04-a6a6-251fd20f1443")
_EVENT_NAMESPACE = UUID("2933fc08-4567-5f5e-876f-0c9eabef6fb7")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class CorporateActionError(ResearchError):
    """Base exception for corporate-action research failures."""


class CorporateActionConfigurationError(
    CorporateActionError,
    ResearchConfigurationError,
):
    """Raised when corporate-action metadata or policy is invalid."""


class CorporateActionConflictError(CorporateActionError, ResearchConflictError):
    """Raised when immutable corporate-action metadata conflicts."""


class CorporateActionIntegrityError(CorporateActionError, ResearchIntegrityError):
    """Raised when stored corporate-action metadata fails integrity checks."""


class CorporateActionNotFoundError(CorporateActionError, ResearchNotFoundError):
    """Raised when required corporate-action metadata does not exist."""


class CorporateActionEligibilityError(CorporateActionError):
    """Raised when an action was not historically knowable or executable."""


class CorporateActionInvariantError(CorporateActionError):
    """Raised when an accounting invariant is violated."""


class CorporateActionType(StrEnum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    SYMBOL_CHANGE = "symbol_change"
    DELISTING = "delisting"


class CorporateActionEventPhase(StrEnum):
    APPLY = "apply"
    DIVIDEND_ENTITLEMENT = "dividend_entitlement"
    DIVIDEND_PAYMENT = "dividend_payment"


class FractionalSharePolicy(StrEnum):
    REJECT = "reject"
    CASH_IN_LIEU = "cash_in_lieu"


@dataclass(frozen=True, slots=True)
class CorporateActionPolicy:
    fractional_share_policy: FractionalSharePolicy = FractionalSharePolicy.REJECT
    require_known_before_event: bool = True
    require_explicit_delisting_value: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.fractional_share_policy, FractionalSharePolicy):
            raise CorporateActionConfigurationError(
                "Fractional-share policy must be a FractionalSharePolicy value."
            )
        if not isinstance(self.require_known_before_event, bool):
            raise CorporateActionConfigurationError(
                "Known-before-event policy flag must be a boolean."
            )
        if self.require_explicit_delisting_value is not True:
            raise CorporateActionConfigurationError(
                "Corporate-action v1 requires explicit delisting settlement."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "fractional_share_policy": self.fractional_share_policy.value,
            "require_known_before_event": self.require_known_before_event,
            "require_explicit_delisting_value": (
                self.require_explicit_delisting_value
            ),
        }


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    exchange: str
    symbol: str
    action_type: CorporateActionType
    effective_at: datetime
    available_at: datetime
    source: str
    source_digest: str
    ratio_numerator: int | None = None
    ratio_denominator: int | None = None
    cash_amount_per_share: Decimal | None = None
    declared_at: datetime | None = None
    ex_at: datetime | None = None
    record_at: datetime | None = None
    payment_at: datetime | None = None
    new_symbol: str | None = None
    cash_in_lieu_price: Decimal | None = None
    delisting_cash_price: Decimal | None = None
    delisting_recovery_rate: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        if not isinstance(self.action_type, CorporateActionType):
            raise CorporateActionConfigurationError(
                "Action type must be a CorporateActionType value."
            )
        _require_aware(self.effective_at, "Effective-at timestamp")
        _require_aware(self.available_at, "Available-at timestamp")
        _require_nonblank(self.source, "Corporate-action source")
        _validate_sha256(self.source_digest, "Corporate-action source digest")
        _require_optional_positive_int(self.ratio_numerator, "Ratio numerator")
        _require_optional_positive_int(self.ratio_denominator, "Ratio denominator")
        _require_optional_positive_decimal(
            self.cash_amount_per_share,
            "Cash amount per share",
        )
        _require_optional_aware(self.declared_at, "Declared-at timestamp")
        _require_optional_aware(self.ex_at, "Ex-dividend timestamp")
        _require_optional_aware(self.record_at, "Record timestamp")
        _require_optional_aware(self.payment_at, "Payment timestamp")
        if self.new_symbol is not None:
            object.__setattr__(
                self,
                "new_symbol",
                _normalized_code(self.new_symbol, "New symbol"),
            )
        _require_optional_positive_decimal(
            self.cash_in_lieu_price,
            "Cash-in-lieu price",
        )
        _require_optional_nonnegative_decimal(
            self.delisting_cash_price,
            "Delisting cash price",
        )
        _require_optional_nonnegative_decimal(
            self.delisting_recovery_rate,
            "Delisting recovery rate",
        )
        self._validate_type_specific_fields()

    @property
    def action_id(self) -> str:
        return _record_id("corporate-action", self.to_document())

    @property
    def natural_key(self) -> tuple[str, str, str, str]:
        return (
            self.exchange,
            self.symbol,
            self.action_type.value,
            format_utc(self.effective_at),
        )

    @property
    def events(self) -> tuple[CorporateActionEvent, ...]:
        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            assert self.ex_at is not None
            assert self.payment_at is not None
            return (
                CorporateActionEvent(
                    action=self,
                    phase=CorporateActionEventPhase.DIVIDEND_ENTITLEMENT,
                    event_at=self.ex_at,
                ),
                CorporateActionEvent(
                    action=self,
                    phase=CorporateActionEventPhase.DIVIDEND_PAYMENT,
                    event_at=self.payment_at,
                ),
            )
        return (
            CorporateActionEvent(
                action=self,
                phase=CorporateActionEventPhase.APPLY,
                event_at=self.effective_at,
            ),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "action_type": self.action_type.value,
            "effective_at": format_utc(self.effective_at),
            "available_at": format_utc(self.available_at),
            "source": self.source,
            "source_digest": self.source_digest,
            "ratio_numerator": self.ratio_numerator,
            "ratio_denominator": self.ratio_denominator,
            "cash_amount_per_share": _optional_decimal_text(
                self.cash_amount_per_share
            ),
            "declared_at": _optional_time_text(self.declared_at),
            "ex_at": _optional_time_text(self.ex_at),
            "record_at": _optional_time_text(self.record_at),
            "payment_at": _optional_time_text(self.payment_at),
            "new_symbol": self.new_symbol,
            "cash_in_lieu_price": _optional_decimal_text(
                self.cash_in_lieu_price
            ),
            "delisting_cash_price": _optional_decimal_text(
                self.delisting_cash_price
            ),
            "delisting_recovery_rate": _optional_decimal_text(
                self.delisting_recovery_rate
            ),
        }

    def _validate_type_specific_fields(self) -> None:
        ratio_fields = (self.ratio_numerator, self.ratio_denominator)
        dividend_fields = (
            self.cash_amount_per_share,
            self.declared_at,
            self.ex_at,
            self.record_at,
            self.payment_at,
        )
        delisting_values = (
            self.delisting_cash_price,
            self.delisting_recovery_rate,
        )
        if self.action_type in (
            CorporateActionType.SPLIT,
            CorporateActionType.REVERSE_SPLIT,
        ):
            if any(value is None for value in ratio_fields):
                raise CorporateActionConfigurationError(
                    "Split actions require numerator and denominator."
                )
            assert self.ratio_numerator is not None
            assert self.ratio_denominator is not None
            if self.action_type is CorporateActionType.SPLIT:
                if self.ratio_numerator <= self.ratio_denominator:
                    raise CorporateActionConfigurationError(
                        "A split ratio must increase the share count."
                    )
            elif self.ratio_numerator >= self.ratio_denominator:
                raise CorporateActionConfigurationError(
                    "A reverse-split ratio must reduce the share count."
                )
            if any(value is not None for value in dividend_fields):
                raise CorporateActionConfigurationError(
                    "Split actions cannot contain dividend fields."
                )
            if self.new_symbol is not None or any(
                value is not None for value in delisting_values
            ):
                raise CorporateActionConfigurationError(
                    "Split actions contain incompatible fields."
                )
            return

        if any(value is not None for value in ratio_fields):
            raise CorporateActionConfigurationError(
                "Only split actions can contain share ratios."
            )
        if self.cash_in_lieu_price is not None:
            raise CorporateActionConfigurationError(
                "Cash-in-lieu price is valid only for split actions."
            )

        if self.action_type is CorporateActionType.CASH_DIVIDEND:
            if any(value is None for value in dividend_fields):
                raise CorporateActionConfigurationError(
                    "Cash dividends require declaration, ex, record, payment, "
                    "and amount."
                )
            assert self.declared_at is not None
            assert self.ex_at is not None
            assert self.record_at is not None
            assert self.payment_at is not None
            if self.available_at < self.declared_at:
                raise CorporateActionConfigurationError(
                    "Dividend metadata cannot be available before declaration."
                )
            if not (
                self.declared_at <= self.ex_at <= self.record_at <= self.payment_at
            ):
                raise CorporateActionConfigurationError(
                    "Dividend timestamps must follow declaration <= ex <= record "
                    "<= payment."
                )
            if self.effective_at != self.ex_at:
                raise CorporateActionConfigurationError(
                    "Cash-dividend effective timestamp must equal its ex timestamp."
                )
            if self.new_symbol is not None or any(
                value is not None for value in delisting_values
            ):
                raise CorporateActionConfigurationError(
                    "Cash dividends contain incompatible fields."
                )
            return

        if any(value is not None for value in dividend_fields):
            raise CorporateActionConfigurationError(
                "Only cash-dividend actions can contain dividend fields."
            )

        if self.action_type is CorporateActionType.SYMBOL_CHANGE:
            if self.new_symbol is None or self.new_symbol == self.symbol:
                raise CorporateActionConfigurationError(
                    "Symbol changes require a distinct successor symbol."
                )
            if any(value is not None for value in delisting_values):
                raise CorporateActionConfigurationError(
                    "Symbol changes cannot contain delisting values."
                )
            return

        if self.new_symbol is not None:
            raise CorporateActionConfigurationError(
                "Only symbol-change actions can contain a successor symbol."
            )
        if self.action_type is CorporateActionType.DELISTING:
            if sum(value is not None for value in delisting_values) != 1:
                raise CorporateActionConfigurationError(
                    "Delisting actions require exactly one explicit settlement value."
                )
            if (
                self.delisting_recovery_rate is not None
                and self.delisting_recovery_rate > _ONE
            ):
                raise CorporateActionConfigurationError(
                    "Delisting recovery rate cannot exceed 1."
                )
            return
        raise CorporateActionConfigurationError("Unsupported corporate-action type.")


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    action: CorporateActionRecord
    phase: CorporateActionEventPhase
    event_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.action, CorporateActionRecord):
            raise CorporateActionConfigurationError(
                "Corporate-action events require a CorporateActionRecord."
            )
        if not isinstance(self.phase, CorporateActionEventPhase):
            raise CorporateActionConfigurationError(
                "Corporate-action event phase is invalid."
            )
        _require_aware(self.event_at, "Corporate-action event timestamp")
        if self.action.action_type is CorporateActionType.CASH_DIVIDEND:
            assert self.action.ex_at is not None
            assert self.action.payment_at is not None
            expected_pairs = {
                (
                    CorporateActionEventPhase.DIVIDEND_ENTITLEMENT,
                    self.action.ex_at,
                ),
                (
                    CorporateActionEventPhase.DIVIDEND_PAYMENT,
                    self.action.payment_at,
                ),
            }
        else:
            expected_pairs = {
                (CorporateActionEventPhase.APPLY, self.action.effective_at)
            }
        if (self.phase, self.event_at) not in expected_pairs:
            raise CorporateActionConfigurationError(
                "Corporate-action event phase and timestamp do not match its action."
            )

    @property
    def event_id(self) -> str:
        return str(
            uuid5(
                _EVENT_NAMESPACE,
                "|".join(
                    (
                        self.action.action_id,
                        self.phase.value,
                        format_utc(self.event_at),
                    )
                ),
            )
        )

    @property
    def priority(self) -> int:
        if self.action.action_type in (
            CorporateActionType.SPLIT,
            CorporateActionType.REVERSE_SPLIT,
        ):
            return 10
        if self.phase is CorporateActionEventPhase.DIVIDEND_ENTITLEMENT:
            return 20
        if self.phase is CorporateActionEventPhase.DIVIDEND_PAYMENT:
            return 30
        if self.action.action_type is CorporateActionType.SYMBOL_CHANGE:
            return 40
        if self.action.action_type is CorporateActionType.DELISTING:
            return 50
        return 60

    def sort_key(self) -> tuple[datetime, int, str]:
        return (self.event_at.astimezone(UTC), self.priority, self.event_id)

    def to_document(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "action_id": self.action.action_id,
            "action_type": self.action.action_type.value,
            "phase": self.phase.value,
            "event_at": format_utc(self.event_at),
        }


@dataclass(frozen=True, slots=True)
class CorporateActionBacktestContext:
    exchange: str
    initial_symbol: str
    start: datetime
    end: datetime
    policy: CorporateActionPolicy
    action_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    action_dataset_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(
            self,
            "initial_symbol",
            _normalized_code(self.initial_symbol, "Initial symbol"),
        )
        _require_aware(self.start, "Corporate-action context start")
        _require_aware(self.end, "Corporate-action context end")
        if self.start >= self.end:
            raise CorporateActionConfigurationError(
                "Corporate-action context requires start before end."
            )
        if not isinstance(self.policy, CorporateActionPolicy):
            raise CorporateActionConfigurationError(
                "Corporate-action context policy is invalid."
            )
        _validate_sorted_unique_uuids(self.action_ids, "Corporate-action IDs")
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise CorporateActionConfigurationError(
                "Corporate-action context requires a symbol path."
            )
        normalized_symbols = tuple(
            _normalized_code(symbol, "Context symbol") for symbol in self.symbols
        )
        if normalized_symbols != self.symbols:
            object.__setattr__(self, "symbols", normalized_symbols)
        if self.symbols[0] != self.initial_symbol:
            raise CorporateActionConfigurationError(
                "Corporate-action symbol path must begin with the initial symbol."
            )
        if len(self.symbols) != len(set(self.symbols)):
            raise CorporateActionConfigurationError(
                "Corporate-action symbol path cannot contain cycles."
            )
        _validate_sha256(
            self.action_dataset_digest,
            "Corporate-action dataset digest",
        )

    @property
    def context_digest(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "exchange": self.exchange,
            "initial_symbol": self.initial_symbol,
            "start": format_utc(self.start),
            "end": format_utc(self.end),
            "policy": self.policy.to_document(),
            "action_ids": list(self.action_ids),
            "symbols": list(self.symbols),
            "action_dataset_digest": self.action_dataset_digest,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionApplication:
    application_id: str
    event_id: str
    action_id: str
    action_type: CorporateActionType
    phase: CorporateActionEventPhase
    applied_at: datetime
    symbol_before: str
    symbol_after: str
    quantity_before: int
    quantity_after: int
    cash_delta: Decimal
    cost_basis_before: Decimal
    cost_basis_after: Decimal
    gross_amount: Decimal = _ZERO
    tax_amount: Decimal = _ZERO
    net_amount: Decimal = _ZERO
    reference_price: Decimal | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.application_id, "Application ID"),
            (self.event_id, "Event ID"),
            (self.action_id, "Action ID"),
        ):
            _validate_uuid(value, field_name)
        if not isinstance(self.action_type, CorporateActionType):
            raise CorporateActionConfigurationError(
                "Application action type is invalid."
            )
        if not isinstance(self.phase, CorporateActionEventPhase):
            raise CorporateActionConfigurationError("Application phase is invalid.")
        _require_aware(self.applied_at, "Application timestamp")
        object.__setattr__(
            self,
            "symbol_before",
            _normalized_code(self.symbol_before, "Application prior symbol"),
        )
        object.__setattr__(
            self,
            "symbol_after",
            _normalized_code(self.symbol_after, "Application resulting symbol"),
        )
        _require_nonnegative_int(self.quantity_before, "Prior quantity")
        _require_nonnegative_int(self.quantity_after, "Resulting quantity")
        _require_finite_decimal(self.cash_delta, "Cash delta")
        for decimal_value, field_name in (
            (self.cost_basis_before, "Prior cost basis"),
            (self.cost_basis_after, "Resulting cost basis"),
            (self.gross_amount, "Gross amount"),
            (self.tax_amount, "Tax amount"),
            (self.net_amount, "Net amount"),
        ):
            _require_nonnegative_decimal(decimal_value, field_name)
        _require_optional_nonnegative_decimal(self.reference_price, "Reference price")
        if self.tax_amount > self.gross_amount:
            raise CorporateActionInvariantError(
                "Corporate-action tax cannot exceed its gross amount."
            )
        if self.net_amount != self.gross_amount - self.tax_amount:
            raise CorporateActionInvariantError(
                "Corporate-action net amount must equal gross less tax."
            )
        if self.cash_delta != self.net_amount and self.phase in (
            CorporateActionEventPhase.DIVIDEND_PAYMENT,
        ):
            raise CorporateActionInvariantError(
                "Dividend-payment cash delta must equal its net amount."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "application_id": self.application_id,
            "event_id": self.event_id,
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "phase": self.phase.value,
            "applied_at": format_utc(self.applied_at),
            "symbol_before": self.symbol_before,
            "symbol_after": self.symbol_after,
            "quantity_before": self.quantity_before,
            "quantity_after": self.quantity_after,
            "cash_delta": format(self.cash_delta, "f"),
            "cost_basis_before": format(self.cost_basis_before, "f"),
            "cost_basis_after": format(self.cost_basis_after, "f"),
            "gross_amount": format(self.gross_amount, "f"),
            "tax_amount": format(self.tax_amount, "f"),
            "net_amount": format(self.net_amount, "f"),
            "reference_price": _optional_decimal_text(self.reference_price),
        }


def corporate_action_policy_json(policy: CorporateActionPolicy) -> str:
    if not isinstance(policy, CorporateActionPolicy):
        raise CorporateActionConfigurationError(
            "Corporate-action policy JSON requires a CorporateActionPolicy."
        )
    return canonical_json_bytes(policy.to_document()).decode("utf-8")


def corporate_action_from_document(
    document: Mapping[str, object],
) -> CorporateActionRecord:
    _require_schema(document)
    return CorporateActionRecord(
        exchange=_required_string(document, "exchange"),
        symbol=_required_string(document, "symbol"),
        action_type=CorporateActionType(_required_string(document, "action_type")),
        effective_at=_parse_utc(_required_string(document, "effective_at")),
        available_at=_parse_utc(_required_string(document, "available_at")),
        source=_required_string(document, "source"),
        source_digest=_required_string(document, "source_digest"),
        ratio_numerator=_optional_int(document, "ratio_numerator"),
        ratio_denominator=_optional_int(document, "ratio_denominator"),
        cash_amount_per_share=_optional_decimal(document, "cash_amount_per_share"),
        declared_at=_optional_datetime(document, "declared_at"),
        ex_at=_optional_datetime(document, "ex_at"),
        record_at=_optional_datetime(document, "record_at"),
        payment_at=_optional_datetime(document, "payment_at"),
        new_symbol=_optional_string(document, "new_symbol"),
        cash_in_lieu_price=_optional_decimal(document, "cash_in_lieu_price"),
        delisting_cash_price=_optional_decimal(document, "delisting_cash_price"),
        delisting_recovery_rate=_optional_decimal(
            document,
            "delisting_recovery_rate",
        ),
    )


def _record_id(record_kind: str, document: Mapping[str, object]) -> str:
    digest = _sha256({"kind": record_kind, "data": document})
    return str(uuid5(_ACTION_NAMESPACE, digest))


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _normalized_code(value: object, field_name: str) -> str:
    _require_nonblank(value, field_name)
    return cast(str, value).strip().upper()


def _optional_time_text(value: datetime | None) -> str | None:
    return None if value is None else format_utc(value)


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CorporateActionConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionConfigurationError(f"{field_name} cannot be empty.")


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CorporateActionConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_optional_positive_int(value: int | None, field_name: str) -> None:
    if value is not None:
        _require_positive_int(value, field_name)


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorporateActionConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_finite_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CorporateActionConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_nonnegative_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO:
        raise CorporateActionConfigurationError(f"{field_name} cannot be negative.")


def _require_optional_nonnegative_decimal(
    value: Decimal | None,
    field_name: str,
) -> None:
    if value is not None:
        _require_nonnegative_decimal(value, field_name)


def _require_optional_positive_decimal(
    value: Decimal | None,
    field_name: str,
) -> None:
    if value is None:
        return
    _require_finite_decimal(value, field_name)
    if value <= _ZERO:
        raise CorporateActionConfigurationError(
            f"{field_name} must be greater than zero."
        )


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise CorporateActionConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise CorporateActionConfigurationError(f"{field_name} must be a UUID string.")
    try:
        UUID(value)
    except ValueError as error:
        raise CorporateActionConfigurationError(
            f"{field_name} must be a valid UUID."
        ) from error


def _validate_sorted_unique_uuids(values: object, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise CorporateActionConfigurationError(f"{field_name} must be a tuple.")
    for value in values:
        _validate_uuid(value, field_name)
    if tuple(sorted(values)) != values or len(values) != len(set(values)):
        raise CorporateActionConfigurationError(
            f"{field_name} must be uniquely and deterministically sorted."
        )


def _require_schema(document: Mapping[str, object]) -> None:
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise CorporateActionIntegrityError(
            "Unsupported corporate-action schema version."
        )


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise CorporateActionIntegrityError(f"Stored field {key!r} must be a string.")
    return value


def _optional_string(document: Mapping[str, object], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorporateActionIntegrityError(
            f"Stored field {key!r} must be a string or null."
        )
    return value


def _optional_int(document: Mapping[str, object], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorporateActionIntegrityError(
            f"Stored field {key!r} must be an integer or null."
        )
    return value


def _optional_decimal(document: Mapping[str, object], key: str) -> Decimal | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorporateActionIntegrityError(
            f"Stored field {key!r} must be a decimal string or null."
        )
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise CorporateActionIntegrityError(
            f"Stored field {key!r} contains an invalid decimal."
        ) from error


def _optional_datetime(document: Mapping[str, object], key: str) -> datetime | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorporateActionIntegrityError(
            f"Stored field {key!r} must be a timestamp or null."
        )
    return _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CorporateActionIntegrityError(
            "Stored corporate-action timestamp is invalid."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorporateActionIntegrityError(
            "Stored corporate-action timestamps must include timezone information."
        )
    return parsed.astimezone(UTC)


__all__ = [
    "CorporateActionApplication",
    "CorporateActionBacktestContext",
    "CorporateActionConfigurationError",
    "CorporateActionConflictError",
    "CorporateActionEligibilityError",
    "CorporateActionError",
    "CorporateActionEvent",
    "CorporateActionEventPhase",
    "CorporateActionIntegrityError",
    "CorporateActionInvariantError",
    "CorporateActionNotFoundError",
    "CorporateActionPolicy",
    "CorporateActionRecord",
    "CorporateActionType",
    "FractionalSharePolicy",
    "corporate_action_from_document",
    "corporate_action_policy_json",
]
