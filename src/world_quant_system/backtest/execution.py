from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.interfaces import (
    CommissionModel,
    PositionSizer,
    SlippageModel,
)
from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    BacktestInvariantError,
    ExecutionDecision,
    Fill,
    OrderRejectReason,
    OrderSide,
    PortfolioSnapshot,
    TargetPositionOrder,
)
from world_quant_system.domain import Candle

_BPS_DENOMINATOR = Decimal("10000")
_ZERO = Decimal("0")
_FILL_NAMESPACE = UUID("5e153ec0-d517-5eb5-a1da-b8a9c2fe6f92")


class BasisPointsCommissionModel:
    def __init__(self, basis_points: Decimal) -> None:
        _validate_bps(basis_points, "Commission basis points", allow_full=True)
        self._basis_points = basis_points

    @property
    def basis_points(self) -> Decimal:
        return self._basis_points

    def calculate(self, notional: Decimal) -> Decimal:
        if (
            not isinstance(notional, Decimal)
            or not notional.is_finite()
            or notional < 0
        ):
            raise BacktestConfigurationError(
                "Commission notional must be a finite nonnegative Decimal."
            )
        return notional * self._basis_points / _BPS_DENOMINATOR


class FixedBasisPointsSlippageModel:
    def __init__(self, basis_points: Decimal) -> None:
        _validate_bps(basis_points, "Slippage basis points", allow_full=False)
        self._basis_points = basis_points

    @property
    def basis_points(self) -> Decimal:
        return self._basis_points

    def execution_price(
        self,
        order: TargetPositionOrder,
        reference_price: Decimal,
    ) -> Decimal:
        if not isinstance(reference_price, Decimal) or not reference_price.is_finite():
            raise BacktestConfigurationError(
                "Slippage reference price must be a finite Decimal."
            )
        if reference_price <= _ZERO:
            raise BacktestConfigurationError(
                "Slippage reference price must be greater than zero."
            )
        rate = self._basis_points / _BPS_DENOMINATOR
        multiplier = Decimal("1") + rate
        if order.side is OrderSide.SELL:
            multiplier = Decimal("1") - rate
        price = reference_price * multiplier
        if price <= _ZERO:
            raise BacktestInvariantError(
                "Slippage model produced a nonpositive execution price."
            )
        return price


class LongOnlyTargetPositionSizer:
    """Efficient whole-share sizing for long-or-flat target orders."""

    def requested_quantity(
        self,
        order: TargetPositionOrder,
        portfolio: PortfolioSnapshot,
        execution_price: Decimal,
        commission_model: CommissionModel,
    ) -> int:
        if order.side is OrderSide.SELL:
            return portfolio.quantity
        if portfolio.cash <= _ZERO:
            return 0
        if isinstance(commission_model, BasisPointsCommissionModel):
            rate = commission_model.basis_points / _BPS_DENOMINATOR
            unit_cost = execution_price * (Decimal("1") + rate)
            return int(
                (portfolio.cash / unit_cost).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )

        upper = int(
            (portfolio.cash / execution_price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        lower = 0
        while lower < upper:
            candidate = (lower + upper + 1) // 2
            notional = execution_price * candidate
            total_cost = notional + commission_model.calculate(notional)
            if total_cost <= portfolio.cash:
                lower = candidate
            else:
                upper = candidate - 1
        return lower


class NextOpenExecutionModel:
    """Deterministic long-only next-open execution with volume participation."""

    def __init__(
        self,
        commission_model: CommissionModel,
        slippage_model: SlippageModel,
        *,
        max_volume_participation: Decimal,
        position_sizer: PositionSizer | None = None,
    ) -> None:
        if (
            not isinstance(max_volume_participation, Decimal)
            or not max_volume_participation.is_finite()
            or not Decimal("0") < max_volume_participation <= Decimal("1")
        ):
            raise BacktestConfigurationError(
                "Maximum volume participation must be in the interval (0, 1]."
            )
        self._commission_model = commission_model
        self._slippage_model = slippage_model
        self._position_sizer = position_sizer or LongOnlyTargetPositionSizer()
        self._max_volume_participation = max_volume_participation

    def execute(
        self,
        order: TargetPositionOrder,
        candle: Candle,
        portfolio: PortfolioSnapshot,
    ) -> ExecutionDecision:
        if candle.symbol != order.symbol or portfolio.symbol != order.symbol:
            raise BacktestInvariantError(
                "Order, candle, and portfolio symbols must match."
            )
        if candle.timestamp <= order.requested_at:
            raise BacktestInvariantError(
                "Orders must execute strictly after their signal timestamp."
            )

        participating_volume = (
            Decimal(candle.volume) * self._max_volume_participation
        )
        volume_limit = int(
            participating_volume.to_integral_value(rounding=ROUND_FLOOR)
        )
        if volume_limit <= 0:
            return ExecutionDecision(
                fill=None,
                reject_reason=OrderRejectReason.NO_EXECUTABLE_VOLUME,
                requested_quantity=0,
                target_quantity=portfolio.quantity,
            )

        reference_price = candle.open_price
        execution_price = self._slippage_model.execution_price(
            order,
            reference_price,
        )

        requested_quantity = self._position_sizer.requested_quantity(
            order,
            portfolio,
            execution_price,
            self._commission_model,
        )
        if order.side is OrderSide.BUY:
            if requested_quantity <= 0:
                return ExecutionDecision(
                    fill=None,
                    reject_reason=OrderRejectReason.INSUFFICIENT_CASH,
                    requested_quantity=0,
                    target_quantity=portfolio.quantity,
                )
            quantity = min(requested_quantity, volume_limit)
            target_quantity = portfolio.quantity + requested_quantity
        else:
            if requested_quantity <= 0:
                return ExecutionDecision(
                    fill=None,
                    reject_reason=OrderRejectReason.NO_POSITION,
                    requested_quantity=0,
                    target_quantity=0,
                )
            quantity = min(requested_quantity, volume_limit)
            target_quantity = 0

        if quantity <= 0:
            return ExecutionDecision(
                fill=None,
                reject_reason=OrderRejectReason.ZERO_QUANTITY,
                requested_quantity=requested_quantity,
                target_quantity=target_quantity,
            )

        notional = execution_price * quantity
        commission = self._commission_model.calculate(notional)
        if order.side is OrderSide.BUY and notional + commission > portfolio.cash:
            raise BacktestInvariantError(
                "Affordable-quantity calculation exceeded available cash."
            )
        slippage_cost = abs(execution_price - reference_price) * quantity
        fill_id = str(
            uuid5(
                _FILL_NAMESPACE,
                "|".join(
                    (
                        order.order_id,
                        candle.timestamp.isoformat(),
                        str(quantity),
                        format(execution_price, "f"),
                    )
                ),
            )
        )
        fill = Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            reference_price=reference_price,
            execution_price=execution_price,
            notional=notional,
            commission=commission,
            slippage_cost=slippage_cost,
            filled_at=candle.timestamp,
        )
        return ExecutionDecision(
            fill=fill,
            reject_reason=None,
            requested_quantity=requested_quantity,
            target_quantity=target_quantity,
        )


def _validate_bps(value: Decimal, field_name: str, *, allow_full: bool) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
        raise BacktestConfigurationError(
            f"{field_name} must be a finite nonnegative Decimal."
        )
    upper_bound = _BPS_DENOMINATOR if allow_full else _BPS_DENOMINATOR - Decimal("1")
    if value > upper_bound:
        comparator = "10000" if allow_full else "less than 10000"
        raise BacktestConfigurationError(
            f"{field_name} must be {comparator} basis points or lower."
        )
