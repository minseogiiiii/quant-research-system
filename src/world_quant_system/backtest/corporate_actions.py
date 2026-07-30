from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from world_quant_system.backtest.models import BacktestConfigurationError
from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.research.corporate_action_models import (
    CorporateActionBacktestContext,
    CorporateActionEligibilityError,
    CorporateActionEvent,
    CorporateActionIntegrityError,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_CURRENCY_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class FlatRateDividendTaxModel:
    """Apply one deterministic withholding rate to gross cash dividends."""

    rate: Decimal = _ZERO

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rate, Decimal)
            or not self.rate.is_finite()
            or not _ZERO <= self.rate <= _ONE
        ):
            raise BacktestConfigurationError(
                "Dividend withholding rate must be a finite Decimal from 0 to 1."
            )

    @property
    def fingerprint(self) -> str:
        document = {
            "model": "flat-rate-dividend-tax",
            "version": 1,
            "rate": format(self.rate, "f"),
            "rounding": "ROUND_HALF_EVEN",
            "quantum": format(_CURRENCY_QUANTUM, "f"),
        }
        return hashlib.sha256(canonical_json_bytes(document)).hexdigest()

    def calculate(self, gross_dividend: Decimal) -> Decimal:
        if (
            not isinstance(gross_dividend, Decimal)
            or not gross_dividend.is_finite()
            or gross_dividend < _ZERO
        ):
            raise BacktestConfigurationError(
                "Gross dividend must be a finite nonnegative Decimal."
            )
        return (gross_dividend * self.rate).quantize(
            _CURRENCY_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )


class CorporateActionTimeline:
    """Deterministic, single-security event stream pinned by one context digest."""

    def __init__(
        self,
        context: CorporateActionBacktestContext,
        actions: tuple[CorporateActionRecord, ...],
    ) -> None:
        if not isinstance(context, CorporateActionBacktestContext):
            raise CorporateActionIntegrityError(
                "Corporate-action timeline requires a validated context."
            )
        if not isinstance(actions, tuple) or not all(
            isinstance(action, CorporateActionRecord) for action in actions
        ):
            raise CorporateActionIntegrityError(
                "Corporate-action timeline actions must be an immutable tuple."
            )
        supplied_ids = tuple(sorted(action.action_id for action in actions))
        if supplied_ids != context.action_ids:
            raise CorporateActionIntegrityError(
                "Corporate-action timeline does not match its context action IDs."
            )
        action_documents = [
            action.to_document()
            for action in sorted(actions, key=lambda item: item.action_id)
        ]
        dataset_digest = hashlib.sha256(
            canonical_json_bytes(action_documents)
        ).hexdigest()
        if dataset_digest != context.action_dataset_digest:
            raise CorporateActionIntegrityError(
                "Corporate-action timeline dataset digest does not match its context."
            )
        events = tuple(
            sorted(
                (event for action in actions for event in action.events),
                key=lambda event: event.sort_key(),
            )
        )
        self._validate_events(context, events)
        self._context = context
        self._actions = actions
        self._events = events
        self._index = 0

    @property
    def context(self) -> CorporateActionBacktestContext:
        return self._context

    @property
    def policy(self) -> CorporateActionPolicy:
        return self._context.policy

    @property
    def context_digest(self) -> str:
        return self._context.context_digest

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._context.symbols

    @property
    def actions(self) -> tuple[CorporateActionRecord, ...]:
        return self._actions

    @property
    def exhausted(self) -> bool:
        return self._index == len(self._events)

    def reset(self) -> None:
        self._index = 0

    def events_through(self, timestamp: datetime) -> tuple[CorporateActionEvent, ...]:
        _require_aware(timestamp)
        normalized = timestamp.astimezone(UTC)
        ready: list[CorporateActionEvent] = []
        while self._index < len(self._events):
            event = self._events[self._index]
            if event.event_at.astimezone(UTC) > normalized:
                break
            ready.append(event)
            self._index += 1
        return tuple(ready)

    def require_exhausted_through(self, timestamp: datetime) -> None:
        _require_aware(timestamp)
        normalized = timestamp.astimezone(UTC)
        if self._index < len(self._events):
            next_event = self._events[self._index]
            if next_event.event_at.astimezone(UTC) <= normalized:
                raise CorporateActionIntegrityError(
                    "Corporate-action events remained unapplied through the "
                    "requested time."
                )

    @staticmethod
    def _validate_events(
        context: CorporateActionBacktestContext,
        events: tuple[CorporateActionEvent, ...],
    ) -> None:
        current_symbol = context.initial_symbol
        delisted = False
        for event in events:
            action = event.action
            if not context.start <= event.event_at < context.end:
                raise CorporateActionIntegrityError(
                    "Corporate-action event falls outside its backtest context."
                )
            if action.symbol != current_symbol:
                raise CorporateActionIntegrityError(
                    "Corporate actions do not form one continuous symbol path."
                )
            if delisted:
                raise CorporateActionIntegrityError(
                    "No corporate action may occur after delisting."
                )
            first_event_at = min(item.event_at for item in action.events)
            if (
                context.policy.require_known_before_event
                and action.available_at > first_event_at
            ):
                raise CorporateActionEligibilityError(
                    "Corporate action was not known before its first event."
                )
            if action.action_type is CorporateActionType.SYMBOL_CHANGE:
                assert action.new_symbol is not None
                current_symbol = action.new_symbol
            elif action.action_type is CorporateActionType.DELISTING:
                delisted = True
        if current_symbol != context.symbols[-1]:
            raise CorporateActionIntegrityError(
                "Corporate-action symbol path does not match its context."
            )


def _require_aware(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CorporateActionIntegrityError(
            "Corporate-action timeline timestamps must include timezone information."
        )


__all__ = ["CorporateActionTimeline", "FlatRateDividendTaxModel"]
