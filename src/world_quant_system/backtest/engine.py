from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.execution import (
    BasisPointsCommissionModel,
    FixedBasisPointsSlippageModel,
    NextOpenExecutionModel,
)
from world_quant_system.backtest.interfaces import ExecutionModel, Strategy
from world_quant_system.backtest.metrics import calculate_performance_metrics
from world_quant_system.backtest.models import (
    BacktestConfig,
    BacktestConfigurationError,
    BacktestInvariantError,
    BacktestRunResult,
    ClosedTrade,
    ExecutionDecision,
    Fill,
    OrderRecord,
    OrderRejectReason,
    OrderSide,
    OrderStatus,
    PerformanceMetrics,
    PortfolioSnapshot,
    TargetPositionOrder,
    TargetPositionSignal,
    fill_document,
    metrics_document,
    order_document,
    signal_document,
    snapshot_document,
    trade_document,
)
from world_quant_system.backtest.portfolio import LongOnlyPortfolioLedger
from world_quant_system.data.normalized_models import (
    NormalizedMarketDataReader,
    canonical_json_bytes,
)
from world_quant_system.replay import (
    DeterministicReplayEngine,
    ReplayClock,
    ReplayEvent,
    ReplayEventHandler,
)

_ORDER_NAMESPACE = UUID("de0b45b4-c402-5518-b068-a3d9ed401a78")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class StrategyBacktestEngine:
    """Single-symbol deterministic long-only backtest with next-open fills."""

    def __init__(
        self,
        reader: NormalizedMarketDataReader,
        config: BacktestConfig,
        strategy: Strategy,
        *,
        execution_model: ExecutionModel | None = None,
    ) -> None:
        if not isinstance(config, BacktestConfig):
            raise BacktestConfigurationError(
                "Backtest engine requires a BacktestConfig."
            )
        self._reader = reader
        self._config = config
        self._strategy = strategy
        self._execution_model = execution_model or NextOpenExecutionModel(
            BasisPointsCommissionModel(config.commission_bps),
            FixedBasisPointsSlippageModel(config.slippage_bps),
            max_volume_participation=config.max_volume_participation,
        )
        self._has_run = False

    async def run(self) -> BacktestRunResult:
        if self._has_run:
            raise BacktestConfigurationError(
                "Backtest engine instances are single-use to prevent state leakage."
            )
        self._has_run = True
        self._strategy.reset()
        portfolio = LongOnlyPortfolioLedger(
            self._config.symbol,
            self._config.initial_cash,
        )
        handler = _BacktestHandler(
            config=self._config,
            strategy=self._strategy,
            execution_model=self._execution_model,
            portfolio=portfolio,
        )
        replay_result = await DeterministicReplayEngine(
            self._reader,
            self._config.replay,
        ).run(handler)
        handler.expire_pending_order()
        equity_curve = tuple(handler.equity_curve)
        trades = portfolio.closed_trades
        metrics = calculate_performance_metrics(
            initial_equity=self._config.initial_cash,
            equity_curve=equity_curve,
            trades=trades,
            turnover_notional=portfolio.turnover_notional,
            annualization_periods=self._config.annualization_periods,
            benchmark_start_price=handler.benchmark_start_price,
            benchmark_end_price=handler.benchmark_end_price,
        )
        run_digest = _run_digest(
            config=self._config,
            strategy_fingerprint=self._strategy.descriptor.fingerprint,
            replay_digest=replay_result.event_digest,
            signals=tuple(handler.signals),
            orders=tuple(handler.orders),
            fills=tuple(handler.fills),
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
        )
        return BacktestRunResult(
            config=self._config,
            config_fingerprint=self._config.fingerprint,
            strategy=self._strategy.descriptor,
            replay_result=replay_result,
            signals=tuple(handler.signals),
            orders=tuple(handler.orders),
            fills=tuple(handler.fills),
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            run_digest=run_digest,
        )


class _BacktestHandler(ReplayEventHandler):
    def __init__(
        self,
        *,
        config: BacktestConfig,
        strategy: Strategy,
        execution_model: ExecutionModel,
        portfolio: LongOnlyPortfolioLedger,
    ) -> None:
        self._config = config
        self._strategy = strategy
        self._execution_model = execution_model
        self._portfolio = portfolio
        self._pending_order: TargetPositionOrder | None = None
        self.signals: list[TargetPositionSignal] = []
        self.orders: list[OrderRecord] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[PortfolioSnapshot] = []
        self.benchmark_start_price: Decimal | None = None
        self.benchmark_end_price: Decimal | None = None

    async def on_candle(self, event: ReplayEvent, clock: ReplayClock) -> None:
        if clock.current != event.event_time:
            raise BacktestInvariantError(
                "Replay clock and current event time must match."
            )
        candle = event.record.candle
        if candle.symbol != self._config.symbol:
            raise BacktestInvariantError(
                "Backtest received a candle outside its configured symbol."
            )
        if event.sequence == 1:
            self.benchmark_start_price = candle.open_price
        self.benchmark_end_price = candle.close_price

        if self._pending_order is not None:
            open_snapshot = self._portfolio.snapshot(
                timestamp=event.event_time,
                market_price=candle.open_price,
            )
            decision = self._execution_model.execute(
                self._pending_order,
                candle,
                open_snapshot,
            )
            self._record_execution(self._pending_order, decision)
            self._pending_order = None

        close_snapshot = self._portfolio.snapshot(
            timestamp=event.event_time,
            market_price=candle.close_price,
        )
        self.equity_curve.append(close_snapshot)
        signal = self._strategy.on_candle(event, close_snapshot)
        self._validate_signal(signal, event)
        self.signals.append(signal)
        self._pending_order = self._order_for_signal(signal, close_snapshot)

    def expire_pending_order(self) -> None:
        if self._pending_order is None:
            return
        self.orders.append(
            OrderRecord(
                order=self._pending_order,
                status=OrderStatus.EXPIRED,
                requested_quantity=None,
                target_quantity=None,
                quantity=0,
                reject_reason=OrderRejectReason.NO_NEXT_CANDLE,
                fill_id=None,
            )
        )
        self._pending_order = None

    def _record_execution(
        self,
        order: TargetPositionOrder,
        decision: ExecutionDecision,
    ) -> None:
        if decision.fill is None:
            if decision.reject_reason is None:
                raise BacktestInvariantError(
                    "Rejected execution requires a reject reason."
                )
            self.orders.append(
                OrderRecord(
                    order=order,
                    status=OrderStatus.REJECTED,
                    requested_quantity=decision.requested_quantity,
                    target_quantity=decision.target_quantity,
                    quantity=0,
                    reject_reason=decision.reject_reason,
                    fill_id=None,
                )
            )
            return
        fill = decision.fill
        self._portfolio.apply_fill(fill)
        self.fills.append(fill)
        status = (
            OrderStatus.FILLED
            if fill.quantity >= decision.requested_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        self.orders.append(
            OrderRecord(
                order=order,
                status=status,
                requested_quantity=decision.requested_quantity,
                target_quantity=decision.target_quantity,
                quantity=fill.quantity,
                reject_reason=None,
                fill_id=fill.fill_id,
            )
        )

    def _validate_signal(
        self,
        signal: TargetPositionSignal,
        event: ReplayEvent,
    ) -> None:
        if not isinstance(signal, TargetPositionSignal):
            raise BacktestInvariantError(
                "Strategies must return a TargetPositionSignal for every candle."
            )
        if signal.symbol != event.record.candle.symbol:
            raise BacktestInvariantError(
                "Strategy signal symbol must match the current candle."
            )
        if signal.generated_at != event.event_time:
            raise BacktestInvariantError(
                "Strategy signals must be timestamped at the current candle close."
            )

    def _order_for_signal(
        self,
        signal: TargetPositionSignal,
        snapshot: PortfolioSnapshot,
    ) -> TargetPositionOrder | None:
        side: OrderSide
        if signal.target_fraction == _ONE:
            if snapshot.cash < snapshot.market_price:
                return None
            side = OrderSide.BUY
        elif signal.target_fraction == _ZERO:
            if snapshot.quantity == 0:
                return None
            side = OrderSide.SELL
        else:
            raise BacktestInvariantError(
                "Backtest v1 supports only long-or-flat strategy targets."
            )
        order_id = str(
            uuid5(
                _ORDER_NAMESPACE,
                "|".join(
                    (
                        self._strategy.descriptor.fingerprint,
                        signal.signal_id,
                        side.value,
                    )
                ),
            )
        )
        return TargetPositionOrder(
            order_id=order_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=side,
            requested_at=signal.generated_at,
            target_fraction=signal.target_fraction,
            reference_price=snapshot.market_price,
        )


def _run_digest(
    *,
    config: BacktestConfig,
    strategy_fingerprint: str,
    replay_digest: str,
    signals: tuple[TargetPositionSignal, ...],
    orders: tuple[OrderRecord, ...],
    fills: tuple[Fill, ...],
    trades: tuple[ClosedTrade, ...],
    equity_curve: tuple[PortfolioSnapshot, ...],
    metrics: PerformanceMetrics,
) -> str:
    document = {
        "config_fingerprint": config.fingerprint,
        "strategy_fingerprint": strategy_fingerprint,
        "replay_digest": replay_digest,
        "signals": [signal_document(signal) for signal in signals],
        "orders": [order_document(order) for order in orders],
        "fills": [fill_document(fill) for fill in fills],
        "trades": [trade_document(trade) for trade in trades],
        "equity_curve": [snapshot_document(point) for point in equity_curve],
        "metrics": metrics_document(metrics),
    }
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()
