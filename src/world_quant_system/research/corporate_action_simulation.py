from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from world_quant_system.backtest import (
    BacktestConfig,
    BacktestRunResult,
    BuyAndHoldStrategy,
    CorporateActionTimeline,
    FlatRateDividendTaxModel,
    StrategyBacktestEngine,
)
from world_quant_system.backtest.simulation import InMemoryNormalizedCandleReader
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig
from world_quant_system.research.corporate_action_models import (
    CorporateActionBacktestContext,
    CorporateActionConflictError,
    CorporateActionEligibilityError,
    CorporateActionRecord,
    CorporateActionType,
)
from world_quant_system.research.corporate_action_store import (
    SQLiteCorporateActionStore,
)

_NAMESPACE = UUID("b36e6de7-6764-5378-a9ef-53ea979e76a1")
_START = datetime(2020, 1, 1, tzinfo=UTC)
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class CorporateActionSimulationResult:
    action_count: int
    application_count: int
    deterministic_context_digest: bool
    deterministic_run_digest: bool
    idempotent_writes: bool
    conflict_blocked: bool
    future_action_blocked: bool
    split_equity_preserved: bool
    dividend_net: Decimal
    delisting_loss_applied: bool
    final_equity: Decimal


async def run_corporate_action_simulation() -> CorporateActionSimulationResult:
    actions = _actions()
    records = _records()

    async def populate(
        root: Path,
        *,
        reverse: bool,
    ) -> tuple[SQLiteCorporateActionStore, bool]:
        store = SQLiteCorporateActionStore(root)
        ordered = tuple(reversed(actions)) if reverse else actions
        first_results = [await store.save_action(action) for action in ordered]
        second_results = [await store.save_action(action) for action in ordered]
        return store, first_results == second_results

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first_store, first_idempotent = await populate(
            root / "first",
            reverse=False,
        )
        second_store, second_idempotent = await populate(
            root / "second",
            reverse=True,
        )
        first_context = await first_store.build_backtest_context(
            exchange="XNAS",
            initial_symbol="OLD",
            start=_START,
            end=_START + timedelta(days=8),
            expected_delisted_at=_START + timedelta(days=7),
        )
        second_context = await second_store.build_backtest_context(
            exchange="XNAS",
            initial_symbol="OLD",
            start=_START,
            end=_START + timedelta(days=8),
            expected_delisted_at=_START + timedelta(days=7),
        )
        first_actions = await first_store.load_context_actions(first_context)
        second_actions = await second_store.load_context_actions(second_context)
        tax_model = FlatRateDividendTaxModel(Decimal("0.10"))
        first_result = await _run_backtest(
            records,
            first_context,
            first_actions,
            tax_model,
        )
        second_result = await _run_backtest(
            records,
            second_context,
            second_actions,
            tax_model,
        )

        conflict_blocked = False
        try:
            await first_store.save_action(
                CorporateActionRecord(
                    exchange="XNAS",
                    symbol="OLD",
                    action_type=CorporateActionType.SPLIT,
                    effective_at=_START + timedelta(days=2),
                    available_at=_START,
                    source="conflict",
                    source_digest="f" * 64,
                    ratio_numerator=3,
                    ratio_denominator=1,
                )
            )
        except CorporateActionConflictError:
            conflict_blocked = True

        future_store = SQLiteCorporateActionStore(root / "future")
        await future_store.save_action(
            CorporateActionRecord(
                exchange="XNAS",
                symbol="OLD",
                action_type=CorporateActionType.SPLIT,
                effective_at=_START + timedelta(days=2),
                available_at=_START + timedelta(days=3),
                source="future",
                source_digest="9" * 64,
                ratio_numerator=2,
                ratio_denominator=1,
            )
        )
        future_action_blocked = False
        try:
            await future_store.build_backtest_context(
                exchange="XNAS",
                initial_symbol="OLD",
                start=_START,
                end=_START + timedelta(days=4),
            )
        except CorporateActionEligibilityError:
            future_action_blocked = True

        split_application = next(
            application
            for application in first_result.corporate_actions
            if application.action_type is CorporateActionType.SPLIT
        )
        split_equity_preserved = (
            split_application.quantity_before == 100
            and split_application.quantity_after == 200
            and split_application.cost_basis_before == Decimal("1000")
            and split_application.cost_basis_after == Decimal("1000")
            and split_application.cash_delta == _ZERO
        )
        final_snapshot = first_result.equity_curve[-1]
        trade = first_result.trades[0]
        return CorporateActionSimulationResult(
            action_count=len(first_actions),
            application_count=len(first_result.corporate_actions),
            deterministic_context_digest=(
                first_context.context_digest == second_context.context_digest
            ),
            deterministic_run_digest=(
                first_result.run_digest == second_result.run_digest
            ),
            idempotent_writes=first_idempotent and second_idempotent,
            conflict_blocked=conflict_blocked,
            future_action_blocked=future_action_blocked,
            split_equity_preserved=split_equity_preserved,
            dividend_net=final_snapshot.total_dividend_net,
            delisting_loss_applied=(
                final_snapshot.quantity == 0
                and trade.net_pnl == Decimal("-910.00000000")
            ),
            final_equity=final_snapshot.total_equity,
        )


async def _run_backtest(
    records: tuple[NormalizedCandleRecord, ...],
    context: CorporateActionBacktestContext,
    actions: tuple[CorporateActionRecord, ...],
    tax_model: FlatRateDividendTaxModel,
) -> BacktestRunResult:
    timeline = CorporateActionTimeline(context, actions)
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=context.symbols,
            interval=CandleInterval.DAY_1,
            start=context.start,
            end=context.end,
        ),
        initial_symbol=context.initial_symbol,
        initial_cash=Decimal("1000"),
        commission_bps=_ZERO,
        slippage_bps=_ZERO,
        max_volume_participation=Decimal("1"),
        corporate_action_context_digest=context.context_digest,
        dividend_tax_model_digest=tax_model.fingerprint,
    )
    return await StrategyBacktestEngine(
        InMemoryNormalizedCandleReader(records),
        config,
        BuyAndHoldStrategy(),
        corporate_action_timeline=timeline,
        dividend_tax_model=tax_model,
    ).run()


def _actions() -> tuple[CorporateActionRecord, ...]:
    dividend_ex = _START + timedelta(days=3)
    payment_at = _START + timedelta(days=6)
    return (
        CorporateActionRecord(
            exchange="XNAS",
            symbol="OLD",
            action_type=CorporateActionType.SPLIT,
            effective_at=_START + timedelta(days=2),
            available_at=_START,
            source="simulation",
            source_digest="a" * 64,
            ratio_numerator=2,
            ratio_denominator=1,
        ),
        CorporateActionRecord(
            exchange="XNAS",
            symbol="OLD",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=dividend_ex,
            available_at=_START,
            source="simulation",
            source_digest="b" * 64,
            cash_amount_per_share=Decimal("0.5"),
            declared_at=_START,
            ex_at=dividend_ex,
            record_at=_START + timedelta(days=4),
            payment_at=payment_at,
        ),
        CorporateActionRecord(
            exchange="XNAS",
            symbol="OLD",
            action_type=CorporateActionType.SYMBOL_CHANGE,
            effective_at=payment_at,
            available_at=_START,
            source="simulation",
            source_digest="c" * 64,
            new_symbol="NEW",
        ),
        CorporateActionRecord(
            exchange="XNAS",
            symbol="NEW",
            action_type=CorporateActionType.DELISTING,
            effective_at=_START + timedelta(days=7),
            available_at=_START + timedelta(days=5),
            source="simulation",
            source_digest="d" * 64,
            delisting_cash_price=_ZERO,
        ),
    )


def _records() -> tuple[NormalizedCandleRecord, ...]:
    values = tuple(("OLD", day, "10" if day < 2 else "5") for day in range(6))
    values += (("NEW", 6, "5"),)
    return tuple(
        _record(symbol, day, Decimal(price))
        for symbol, day, price in values
    )


def _record(
    symbol: str,
    day: int,
    price: Decimal,
) -> NormalizedCandleRecord:
    timestamp = _START + timedelta(days=day)
    identity = f"{symbol}|{day}"
    return NormalizedCandleRecord(
        item_id=str(uuid5(_NAMESPACE, f"item|{identity}")),
        candle=Candle(
            symbol=symbol,
            interval=CandleInterval.DAY_1,
            timestamp=timestamp,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1_000_000,
            currency="USD",
            source="simulation",
        ),
        quality_status=QualityStatus.PASS,
        raw_record_id=str(uuid5(_NAMESPACE, f"raw|{identity}")),
        raw_content_sha256=f"{day + 1:064x}",
        quality_report_id=str(uuid5(_NAMESPACE, f"report|{identity}")),
        normalized_at=timestamp + timedelta(hours=1),
        normalizer_version="1.0.0",
        content_sha256=f"{day + 100:064x}",
        schema_version=1,
        lineage_count=1,
    )


def main() -> None:
    result = asyncio.run(run_corporate_action_simulation())
    print(f"Corporate-action records: {result.action_count}")
    print(f"Corporate-action applications: {result.application_count}")
    print(
        "Deterministic corporate-action context digest: "
        f"{'match' if result.deterministic_context_digest else 'mismatch'}"
    )
    print(
        "Deterministic corporate-action backtest digest: "
        f"{'match' if result.deterministic_run_digest else 'mismatch'}"
    )
    print(
        "Idempotent corporate-action writes: "
        f"{'yes' if result.idempotent_writes else 'no'}"
    )
    print(
        "Conflicting corporate-action rewrite blocked: "
        f"{'yes' if result.conflict_blocked else 'no'}"
    )
    print(
        "Future-known corporate action blocked: "
        f"{'yes' if result.future_action_blocked else 'no'}"
    )
    print(
        "Split equity and basis preserved: "
        f"{'yes' if result.split_equity_preserved else 'no'}"
    )
    print(f"Net cash dividends: {result.dividend_net}")
    print(
        "Delisting loss applied: "
        f"{'yes' if result.delisting_loss_applied else 'no'}"
    )
    print(f"Final equity after zero-value delisting: {result.final_equity}")
    print("Live trading and broker orders: disabled")
    if not all(
        (
            result.action_count == 4,
            result.application_count == 5,
            result.deterministic_context_digest,
            result.deterministic_run_digest,
            result.idempotent_writes,
            result.conflict_blocked,
            result.future_action_blocked,
            result.split_equity_preserved,
            result.dividend_net == Decimal("90.00000000"),
            result.delisting_loss_applied,
            result.final_equity == Decimal("90.00000000"),
        )
    ):
        raise RuntimeError("Corporate-action economics simulation failed.")


if __name__ == "__main__":
    main()
