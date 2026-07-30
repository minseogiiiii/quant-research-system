from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.corporate_actions import (
    CorporateActionTimeline,
    FlatRateDividendTaxModel,
)
from world_quant_system.backtest.execution import (
    BasisPointsCommissionModel,
    FixedBasisPointsSlippageModel,
    NextOpenExecutionModel,
)
from world_quant_system.backtest.interfaces import (
    DividendTaxModel,
    ExecutionModel,
    Strategy,
)
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
from world_quant_system.research.corporate_action_models import (
    CorporateActionApplication,
    CorporateActionType,
)

_ORDER_NAMESPACE = UUID("de0b45b4-c402-5518-b068-a3d9ed401a78")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class StrategyBacktestEngine:
    """Deterministic long-only backtest with next-open execution."""

    def __init__(
        self,
        reader: NormalizedMarketDataReader,
        config: BacktestConfig,
        strategy: Strategy,
        *,
        execution_model: ExecutionModel | None = None,
        corporate_action_timeline: CorporateActionTimeline | None = None,
        dividend_tax_model: DividendTaxModel | None = None,
    ) -> None:
        if not isinstance(config, BacktestConfig):
            raise BacktestConfigurationError(
                "Backtest engine requires a BacktestConfig."
            )
        if corporate_action_timeline is None:
            if config.corporate_action_context_digest is not None:
                raise BacktestConfigurationError(
                    "Configured corporate-action digest requires a timeline."
                )
            if dividend_tax_model is not None:
                raise BacktestConfigurationError(
                    "Dividend tax model requires a corporate-action timeline."
                )
        else:
            if (
                config.corporate_action_context_digest
                != corporate_action_timeline.context_digest
            ):
                raise BacktestConfigurationError(
                    "Corporate-action context digest does not match the timeline."
                )
            configured_tax_model = (
                dividend_tax_model or FlatRateDividendTaxModel()
            )
            if (
                config.dividend_tax_model_digest
                != configured_tax_model.fingerprint
            ):
                raise BacktestConfigurationError(
                    "Dividend-tax digest does not match the configured model."
                )
            if config.replay.symbols != tuple(
                sorted(corporate_action_timeline.symbols)
            ):
                raise BacktestConfigurationError(
                    "Replay symbols must equal the corporate-action symbol path."
                )
            dividend_tax_model = configured_tax_model
        self._reader = reader
        self._config = config
        self._strategy = strategy
        self._execution_model = execution_model or NextOpenExecutionModel(
            BasisPointsCommissionModel(config.commission_bps),
            FixedBasisPointsSlippageModel(config.slippage_bps),
            max_volume_participation=config.max_volume_participation,
        )
        self._timeline = corporate_action_timeline
        self._dividend_tax_model = dividend_tax_model
        self._has_run = False

    async def run(self) -> BacktestRunResult:
        if self._has_run:
            raise BacktestConfigurationError(
                "Backtest engine instances are single-use to prevent state leakage."
            )
        self._has_run = True
        self._strategy.reset()
        if self._timeline is not None:
            self._timeline.reset()
        portfolio = LongOnlyPortfolioLedger(
            self._config.symbol,
            self._config.initial_cash,
        )
        handler = _BacktestHandler(
            config=self._config,
            strategy=self._strategy,
            execution_model=self._execution_model,
            portfolio=portfolio,
            timeline=self._timeline,
            dividend_tax_model=self._dividend_tax_model,
        )
        replay_result = await DeterministicReplayEngine(
            self._reader,
            self._config.replay,
        ).run(handler)
        handler.finalize_corporate_actions(
            self._config.replay.end or replay_result.last_event_at
        )
        handler.expire_pending_order()
        equity_curve = tuple(handler.equity_curve)
        trades = portfolio.closed_trades
        benchmark_start = handler.benchmark_start_price
        benchmark_end = handler.benchmark_end_price
        if self._timeline is not None:
            benchmark_start = None
            benchmark_end = None
        metrics = calculate_performance_metrics(
            initial_equity=self._config.initial_cash,
            equity_curve=equity_curve,
            trades=trades,
            turnover_notional=portfolio.turnover_notional,
            annualization_periods=self._config.annualization_periods,
            benchmark_start_price=benchmark_start,
            benchmark_end_price=benchmark_end,
        )
        corporate_actions = tuple(handler.corporate_actions)
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
            corporate_actions=corporate_actions,
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
            corporate_actions=corporate_actions,
        )


class _BacktestHandler(ReplayEventHandler):
    def __init__(
        self,
        *,
        config: BacktestConfig,
        strategy: Strategy,
        execution_model: ExecutionModel,
        portfolio: LongOnlyPortfolioLedger,
        timeline: CorporateActionTimeline | None,
        dividend_tax_model: DividendTaxModel | None,
    ) -> None:
        self._config = config
        self._strategy = strategy
        self._execution_model = execution_model
        self._portfolio = portfolio
        self._timeline = timeline
        self._dividend_tax_model = dividend_tax_model
        self._pending_order: TargetPositionOrder | None = None
        self._last_market_price: Decimal | None = None
        self.signals: list[TargetPositionSignal] = []
        self.orders: list[OrderRecord] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[PortfolioSnapshot] = []
        self.corporate_actions: list[CorporateActionApplication] = []
        self.benchmark_start_price: Decimal | None = None
        self.benchmark_end_price: Decimal | None = None

    async def on_candle(self, event: ReplayEvent, clock: ReplayClock) -> None:
        if clock.current != event.event_time:
            raise BacktestInvariantError(
                "Replay clock and current event time must match."
            )
        self._apply_corporate_actions_through(event.event_time)
        candle = event.record.candle
        if candle.symbol != self._portfolio.symbol:
            raise BacktestInvariantError(
                "Backtest candle does not match the current portfolio symbol."
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
        self._last_market_price = candle.close_price
        signal = self._strategy.on_candle(event, close_snapshot)
        self._validate_signal(signal, event)
        self.signals.append(signal)
        self._pending_order = self._order_for_signal(signal, close_snapshot)

    def finalize_corporate_actions(self, through: datetime | None) -> None:
        if through is None or self._timeline is None:
            return
        prior_count = len(self.corporate_actions)
        self._apply_corporate_actions_through(through)
        self._timeline.require_exhausted_through(through)
        if len(self.corporate_actions) == prior_count:
            return
        if not self.equity_curve or self._last_market_price is None:
            raise BacktestInvariantError(
                "Post-replay corporate actions require a prior market price."
            )
        self.equity_curve[-1] = self._portfolio.snapshot(
            timestamp=through,
            market_price=self._last_market_price,
        )

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

    def _apply_corporate_actions_through(self, timestamp: datetime) -> None:
        if self._timeline is None:
            return
        if self._dividend_tax_model is None:
            raise BacktestInvariantError(
                "Corporate-action timeline requires a dividend tax model."
            )
        for event in self._timeline.events_through(timestamp):
            if event.action.action_type in (
                CorporateActionType.SYMBOL_CHANGE,
                CorporateActionType.DELISTING,
            ):
                self._reject_pending_for_corporate_action()
            application = self._portfolio.apply_corporate_action(
                event,
                policy=self._timeline.policy,
                dividend_tax_model=self._dividend_tax_model,
                reference_price=self._last_market_price,
            )
            self.corporate_actions.append(application)

    def _reject_pending_for_corporate_action(self) -> None:
        if self._pending_order is None:
            return
        self.orders.append(
            OrderRecord(
                order=self._pending_order,
                status=OrderStatus.REJECTED,
                requested_quantity=0,
                target_quantity=0,
                quantity=0,
                reject_reason=OrderRejectReason.CORPORATE_ACTION_BOUNDARY,
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
    corporate_actions: tuple[CorporateActionApplication, ...],
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
        "corporate_actions": [
            application.to_document() for application in corporate_actions
        ],
    }
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()
