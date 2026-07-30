from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

from world_quant_system.backtest import (
    AtomicJsonBacktestSummaryWriter,
    BacktestConfig,
    BacktestConfigurationError,
    BacktestError,
    BacktestRunResult,
    BuyAndHoldStrategy,
    CorporateActionTimeline,
    FlatRateDividendTaxModel,
    OrderStatus,
    SmaCrossoverStrategy,
    Strategy,
    StrategyBacktestEngine,
)
from world_quant_system.data import (
    NormalizedMarketDataError,
    NormalizedMarketDataReader,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import CandleInterval
from world_quant_system.replay import ReplayConfig, ReplayError
from world_quant_system.research import (
    CorporateActionBacktestContext,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    DataAvailabilityRecord,
    DelistingReason,
    DelistingRecord,
    ExperimentOutcome,
    ExperimentRecord,
    ExperimentSnapshot,
    ExperimentSpec,
    ExperimentStatus,
    FractionalSharePolicy,
    HistoricalDatasetImporter,
    HistoricalDatasetImportSpec,
    HistoricalDatasetPolicy,
    HistoricalDatasetSnapshot,
    HoldoutConsumption,
    MissingSessionPolicy,
    ParameterSearchAudit,
    PointInTimeAccessGrant,
    PointInTimeBacktestContext,
    PointInTimeDataKind,
    PointInTimeSnapshot,
    PointInTimeValidatedCandleReader,
    PointInTimeValidatedMultiSymbolCandleReader,
    ResearchError,
    ResearchSplit,
    ResearchWindow,
    SecurityLifecycle,
    SQLiteCorporateActionStore,
    SQLiteExperimentRegistry,
    SQLiteHistoricalDatasetStore,
    SQLitePointInTimeStore,
    UniverseMembership,
    canonical_json_object,
    corporate_action_policy_json,
    point_in_time_policy_json,
)
from world_quant_system.research.robustness import (
    DeterministicRobustnessRunner,
    StandardBacktestRunFactory,
    build_stress_scenarios,
)
from world_quant_system.research.robustness_models import (
    RobustnessPolicy,
    RobustnessReport,
    RobustnessStrategySpec,
    StressScenario,
    WalkForwardFold,
    WalkForwardPlan,
    sma_parameter_grid,
)
from world_quant_system.research.robustness_reporting import (
    AtomicJsonRobustnessReportWriter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs",
        description="Networkless research interface for World Quant System.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backtest = subparsers.add_parser(
        "backtest",
        help="Run a deterministic long-only strategy backtest.",
    )
    backtest.add_argument("--normalized-root", type=Path, required=True)
    backtest.add_argument(
        "--strategy",
        choices=("buy-and-hold", "sma-cross"),
        required=True,
    )
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in CandleInterval),
        default=CandleInterval.DAY_1.value,
    )
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--initial-cash", default="10000000")
    backtest.add_argument("--commission-bps", default="15")
    backtest.add_argument("--slippage-bps", default="10")
    backtest.add_argument("--max-volume-participation", default="0.10")
    backtest.add_argument(
        "--annualization-periods",
        type=int,
        help=(
            "Periods per year for annualized metrics. Defaults to 252 for 1d; "
            "required for intraday data."
        ),
    )
    backtest.add_argument("--short-window", type=int, default=20)
    backtest.add_argument("--long-window", type=int, default=100)
    backtest.add_argument("--page-size", type=int, default=1000)
    backtest.add_argument(
        "--pass-only",
        action="store_true",
        help="Exclude WARNING-quality normalized candles.",
    )
    backtest.add_argument(
        "--json-output",
        type=Path,
        help="Atomically write a compact result summary as JSON.",
    )
    backtest.add_argument(
        "--point-in-time-root",
        type=Path,
        help="Enable fail-closed point-in-time validation from this catalog root.",
    )
    backtest.add_argument("--universe-id")
    backtest.add_argument("--exchange")
    backtest.add_argument(
        "--corporate-action-root",
        type=Path,
        help="Apply a validated corporate-action catalog during replay.",
    )
    backtest.add_argument(
        "--dividend-tax-rate",
        default="0",
        help="Flat dividend withholding rate from 0 to 1.",
    )
    backtest.add_argument(
        "--fractional-share-policy",
        choices=tuple(policy.value for policy in FractionalSharePolicy),
        default=FractionalSharePolicy.REJECT.value,
    )

    robustness = subparsers.add_parser(
        "robustness",
        help="Run deterministic parameter, cost, delay, and walk-forward tests.",
    )
    robustness.add_argument("--normalized-root", type=Path, required=True)
    robustness.add_argument("--symbol", required=True)
    robustness.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in CandleInterval),
        default=CandleInterval.DAY_1.value,
    )
    robustness.add_argument("--historical-dataset-digest", required=True)
    robustness.add_argument(
        "--fold",
        action="append",
        required=True,
        help=(
            "Repeatable six-value fold: train_start,train_end,"
            "validation_start,validation_end,test_start,test_end."
        ),
    )
    robustness.add_argument(
        "--strategy",
        choices=("buy-and-hold", "sma-cross"),
        default="sma-cross",
    )
    robustness.add_argument("--base-short-window", type=int, default=20)
    robustness.add_argument("--base-long-window", type=int, default=100)
    robustness.add_argument("--short-offset", action="append", type=int)
    robustness.add_argument("--long-offset", action="append", type=int)
    robustness.add_argument("--cost-multiplier", action="append")
    robustness.add_argument("--execution-delay", action="append", type=int)
    robustness.add_argument("--initial-cash", default="10000000")
    robustness.add_argument("--commission-bps", default="15")
    robustness.add_argument("--slippage-bps", default="10")
    robustness.add_argument("--max-volume-participation", default="0.10")
    robustness.add_argument("--annualization-periods", type=int)
    robustness.add_argument("--page-size", type=int, default=1000)
    robustness.add_argument("--pass-only", action="store_true")
    robustness.add_argument("--minimum-observations", type=int, default=20)
    robustness.add_argument("--minimum-test-return", default="0")
    robustness.add_argument("--maximum-drawdown", default="-0.50")
    robustness.add_argument("--maximum-return-degradation", default="-0.25")
    robustness.add_argument("--required-pass-rate", default="0.60")
    robustness.add_argument("--uptrend-threshold", default="0.05")
    robustness.add_argument("--downtrend-threshold", default="-0.05")
    robustness.add_argument("--high-volatility-threshold", type=float, default=0.30)
    robustness.add_argument("--json-output", type=Path)

    dataset = subparsers.add_parser(
        "dataset",
        help="Import, validate, freeze, and inspect historical research datasets.",
    )
    dataset_subparsers = dataset.add_subparsers(
        dest="dataset_command",
        required=True,
    )
    import_dataset = dataset_subparsers.add_parser(
        "import",
        help="Import one strict networkless CSV through the trusted data pipeline.",
    )
    import_dataset.add_argument("--root", type=Path, required=True)
    import_dataset.add_argument("--source-file", type=Path, required=True)
    import_dataset.add_argument("--provider", required=True)
    import_dataset.add_argument("--exchange", required=True)
    import_dataset.add_argument("--symbol", required=True)
    import_dataset.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in CandleInterval),
        required=True,
    )
    import_dataset.add_argument("--timezone", required=True)
    import_dataset.add_argument("--currency", required=True)
    import_dataset.add_argument("--code-commit", required=True)
    import_dataset.add_argument("--point-in-time-context-digest", required=True)
    import_dataset.add_argument("--corporate-action-context-digest", required=True)
    import_dataset.add_argument(
        "--missing-session-policy",
        choices=tuple(policy.value for policy in MissingSessionPolicy),
        default=MissingSessionPolicy.REJECT.value,
    )
    import_dataset.add_argument(
        "--holiday",
        action="append",
        default=[],
        help="Expected exchange holiday in YYYY-MM-DD form; repeat as needed.",
    )

    validate_dataset = dataset_subparsers.add_parser(
        "validate",
        help="Verify manifest, raw bytes, quality report, and normalized digest.",
    )
    validate_dataset.add_argument("--root", type=Path, required=True)
    validate_dataset.add_argument("--dataset-id", required=True)

    freeze_dataset = dataset_subparsers.add_parser(
        "freeze",
        help="Freeze an imported immutable dataset for research use.",
    )
    freeze_dataset.add_argument("--root", type=Path, required=True)
    freeze_dataset.add_argument("--dataset-id", required=True)

    inspect_dataset = dataset_subparsers.add_parser(
        "inspect",
        help="Inspect one historical dataset snapshot.",
    )
    inspect_dataset.add_argument("--root", type=Path, required=True)
    inspect_dataset.add_argument("--dataset-id", required=True)

    link_benchmark = dataset_subparsers.add_parser(
        "link-benchmark",
        help="Link two frozen and exactly aligned historical datasets.",
    )
    link_benchmark.add_argument("--root", type=Path, required=True)
    link_benchmark.add_argument("--dataset-id", required=True)
    link_benchmark.add_argument("--benchmark-dataset-id", required=True)

    list_datasets = dataset_subparsers.add_parser(
        "list",
        help="List historical dataset snapshots in deterministic order.",
    )
    list_datasets.add_argument("--root", type=Path, required=True)
    list_datasets.add_argument("--limit", type=int, default=1000)

    corporate_actions = subparsers.add_parser(
        "corporate-actions",
        help="Manage immutable corporate-action and delisting economics.",
    )
    action_subparsers = corporate_actions.add_subparsers(
        dest="corporate_action_command",
        required=True,
    )
    register_action = action_subparsers.add_parser(
        "register",
        help="Register one immutable corporate action.",
    )
    register_action.add_argument("--root", type=Path, required=True)
    register_action.add_argument("--exchange", required=True)
    register_action.add_argument("--symbol", required=True)
    register_action.add_argument(
        "--type",
        choices=tuple(action_type.value for action_type in CorporateActionType),
        required=True,
    )
    register_action.add_argument("--effective-at", required=True)
    register_action.add_argument("--available-at", required=True)
    register_action.add_argument("--source", required=True)
    register_action.add_argument("--source-digest", required=True)
    register_action.add_argument("--ratio-numerator", type=int)
    register_action.add_argument("--ratio-denominator", type=int)
    register_action.add_argument("--cash-amount-per-share")
    register_action.add_argument("--declared-at")
    register_action.add_argument("--ex-at")
    register_action.add_argument("--record-at")
    register_action.add_argument("--payment-at")
    register_action.add_argument("--new-symbol")
    register_action.add_argument("--cash-in-lieu-price")
    register_action.add_argument("--delisting-cash-price")
    register_action.add_argument("--delisting-recovery-rate")

    inspect_action = action_subparsers.add_parser(
        "inspect",
        help="Inspect one immutable corporate action.",
    )
    inspect_action.add_argument("--root", type=Path, required=True)
    inspect_action.add_argument("--action-id", required=True)

    list_actions = action_subparsers.add_parser(
        "list",
        help="List actions for one security and period.",
    )
    list_actions.add_argument("--root", type=Path, required=True)
    list_actions.add_argument("--exchange", required=True)
    list_actions.add_argument("--symbol", required=True)
    list_actions.add_argument("--start")
    list_actions.add_argument("--end")

    action_context = action_subparsers.add_parser(
        "backtest-context",
        help="Build one deterministic corporate-action backtest context.",
    )
    action_context.add_argument("--root", type=Path, required=True)
    action_context.add_argument("--exchange", required=True)
    action_context.add_argument("--symbol", required=True)
    action_context.add_argument("--start", required=True)
    action_context.add_argument("--end", required=True)
    action_context.add_argument("--expected-delisted-at")
    action_context.add_argument(
        "--fractional-share-policy",
        choices=tuple(policy.value for policy in FractionalSharePolicy),
        default=FractionalSharePolicy.REJECT.value,
    )

    research = subparsers.add_parser(
        "research",
        help="Manage deterministic research-validity records.",
    )
    research_subparsers = research.add_subparsers(
        dest="research_command",
        required=True,
    )
    register = research_subparsers.add_parser(
        "register",
        help="Register an immutable experiment specification before execution.",
    )
    register.add_argument("--root", type=Path, required=True)
    register.add_argument("--strategy", required=True)
    register.add_argument("--strategy-version", default="1.0.0")
    register.add_argument("--dataset-digest", required=True)
    register.add_argument("--code-commit", required=True)
    register.add_argument("--parameters-json", default="{}")
    register.add_argument("--cost-model-json", default="{}")
    register.add_argument("--execution-model-json", default="{}")
    register.add_argument("--train-start", required=True)
    register.add_argument("--train-end", required=True)
    register.add_argument("--validation-start", required=True)
    register.add_argument("--validation-end", required=True)
    register.add_argument("--holdout-start", required=True)
    register.add_argument("--holdout-end", required=True)
    register.add_argument("--search-id", default="standalone")
    register.add_argument("--search-space-json", default="{}")
    register.add_argument("--trial-number", type=int, default=1)
    register.add_argument("--total-trials", type=int, default=1)
    register.add_argument("--selection-metric", default="not_applicable")
    register.add_argument("--parent-experiment-id")
    register.add_argument("--change-reason")
    register.add_argument("--point-in-time-context-digest")
    register.add_argument("--point-in-time-policy-json")
    register.add_argument("--corporate-action-context-digest")
    register.add_argument("--corporate-action-policy-json")
    register.add_argument("--dividend-tax-model-digest")

    inspect = research_subparsers.add_parser(
        "inspect",
        help="Inspect an experiment, its outcome, and holdout state.",
    )
    inspect.add_argument("--root", type=Path, required=True)
    inspect.add_argument("--experiment-id", required=True)

    record_outcome = research_subparsers.add_parser(
        "record-outcome",
        help="Persist an immutable success or failure result.",
    )
    record_outcome.add_argument("--root", type=Path, required=True)
    record_outcome.add_argument("--experiment-id", required=True)
    record_outcome.add_argument(
        "--status",
        choices=tuple(status.value for status in ExperimentStatus),
        required=True,
    )
    record_outcome.add_argument("--result-digest")
    record_outcome.add_argument("--failure-reason")

    consume_holdout = research_subparsers.add_parser(
        "consume-holdout",
        help="Record the one permitted untouched-holdout evaluation.",
    )
    consume_holdout.add_argument("--root", type=Path, required=True)
    consume_holdout.add_argument("--experiment-id", required=True)
    consume_holdout.add_argument("--result-digest", required=True)

    point_in_time = subparsers.add_parser(
        "point-in-time",
        help="Manage point-in-time security, universe, and availability metadata.",
    )
    pit_subparsers = point_in_time.add_subparsers(
        dest="point_in_time_command",
        required=True,
    )
    register_security = pit_subparsers.add_parser(
        "register-security",
        help="Register one immutable security lifecycle.",
    )
    register_security.add_argument("--root", type=Path, required=True)
    register_security.add_argument("--exchange", required=True)
    register_security.add_argument("--symbol", required=True)
    register_security.add_argument("--listed-at", required=True)
    register_security.add_argument("--tradable-from", required=True)
    register_security.add_argument("--delisted-at")
    register_security.add_argument("--tradable-until")
    register_security.add_argument("--source", required=True)
    register_security.add_argument("--source-digest", required=True)

    register_membership = pit_subparsers.add_parser(
        "register-membership",
        help="Register one immutable point-in-time universe membership interval.",
    )
    register_membership.add_argument("--root", type=Path, required=True)
    register_membership.add_argument("--universe-id", required=True)
    register_membership.add_argument("--exchange", required=True)
    register_membership.add_argument("--symbol", required=True)
    register_membership.add_argument("--member-from", required=True)
    register_membership.add_argument("--member-until")
    register_membership.add_argument("--available-at", required=True)
    register_membership.add_argument("--source", required=True)
    register_membership.add_argument("--source-digest", required=True)

    register_availability = pit_subparsers.add_parser(
        "register-availability",
        help="Register when one immutable data item became available.",
    )
    register_availability.add_argument("--root", type=Path, required=True)
    register_availability.add_argument("--data-id", required=True)
    register_availability.add_argument(
        "--data-kind",
        choices=tuple(kind.value for kind in PointInTimeDataKind),
        required=True,
    )
    register_availability.add_argument("--exchange", required=True)
    register_availability.add_argument("--symbol", required=True)
    register_availability.add_argument("--effective-at", required=True)
    register_availability.add_argument("--available-at", required=True)
    register_availability.add_argument("--source", required=True)
    register_availability.add_argument("--source-digest", required=True)

    register_delisting = pit_subparsers.add_parser(
        "register-delisting",
        help="Register an explicit immutable delisting record.",
    )
    register_delisting.add_argument("--root", type=Path, required=True)
    register_delisting.add_argument("--exchange", required=True)
    register_delisting.add_argument("--symbol", required=True)
    register_delisting.add_argument("--last-tradable-at", required=True)
    register_delisting.add_argument("--delisted-at", required=True)
    register_delisting.add_argument("--available-at", required=True)
    register_delisting.add_argument(
        "--reason",
        choices=tuple(reason.value for reason in DelistingReason),
        required=True,
    )
    register_delisting.add_argument("--source", required=True)
    register_delisting.add_argument("--source-digest", required=True)

    snapshot = pit_subparsers.add_parser(
        "snapshot",
        help="Build a deterministic point-in-time universe snapshot.",
    )
    snapshot.add_argument("--root", type=Path, required=True)
    snapshot.add_argument("--universe-id", required=True)
    snapshot.add_argument("--as-of", required=True)

    context = pit_subparsers.add_parser(
        "backtest-context",
        help="Build a fail-closed point-in-time context for one backtest.",
    )
    context.add_argument("--root", type=Path, required=True)
    context.add_argument("--universe-id", required=True)
    context.add_argument("--exchange", required=True)
    context.add_argument("--symbol", required=True)
    context.add_argument("--start", required=True)
    context.add_argument("--end", required=True)

    inspect_security = pit_subparsers.add_parser(
        "inspect-security",
        help="Inspect one immutable security lifecycle.",
    )
    inspect_security.add_argument("--root", type=Path, required=True)
    inspect_security.add_argument("--exchange", required=True)
    inspect_security.add_argument("--symbol", required=True)

    validate_access = pit_subparsers.add_parser(
        "validate-access",
        help="Validate one data item at a historical decision timestamp.",
    )
    validate_access.add_argument("--root", type=Path, required=True)
    validate_access.add_argument("--universe-id", required=True)
    validate_access.add_argument("--exchange", required=True)
    validate_access.add_argument("--symbol", required=True)
    validate_access.add_argument("--data-id", required=True)
    validate_access.add_argument("--event-at", required=True)
    validate_access.add_argument("--decision-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "backtest":
            result = asyncio.run(_run_backtest(arguments))
            print(_format_result(result))
            if arguments.json_output is not None:
                AtomicJsonBacktestSummaryWriter(arguments.json_output).write(result)
            return
        if arguments.command == "robustness":
            report = asyncio.run(_run_robustness(arguments))
            print(_format_robustness_report(report))
            if arguments.json_output is not None:
                AtomicJsonRobustnessReportWriter(arguments.json_output).write(report)
            return
        if arguments.command == "dataset":
            print(asyncio.run(_run_dataset(arguments)))
            return
        if arguments.command == "corporate-actions":
            print(asyncio.run(_run_corporate_actions(arguments)))
            return
        if arguments.command == "research":
            print(asyncio.run(_run_research(arguments)))
            return
        if arguments.command == "point-in-time":
            print(asyncio.run(_run_point_in_time(arguments)))
            return
    except (
        BacktestError,
        NormalizedMarketDataError,
        ReplayError,
        ResearchError,
        ValueError,
        OSError,
    ) as error:
        parser.exit(2, f"ERROR: {error}\n")
    _unreachable()


async def _run_backtest(arguments: argparse.Namespace) -> BacktestRunResult:
    root = arguments.normalized_root.expanduser().resolve()
    database = root / "normalized.sqlite3"
    if not database.is_file():
        raise BacktestConfigurationError(
            f"Normalized database does not exist: {database}"
        )
    strategy = _strategy_from_arguments(arguments)
    interval = CandleInterval(arguments.interval)
    start = _parse_time(arguments.start, is_end=False)
    end = _parse_time(arguments.end, is_end=True)
    normalized_store = SQLiteNormalizedMarketDataStore(root)
    replay_symbols: tuple[str, ...] = (str(arguments.symbol).strip().upper(),)
    corporate_action_context: CorporateActionBacktestContext | None = None
    corporate_action_timeline: CorporateActionTimeline | None = None
    corporate_actions: tuple[CorporateActionRecord, ...] = ()
    dividend_tax_model: FlatRateDividendTaxModel | None = None

    if arguments.corporate_action_root is not None:
        if start is None or end is None or arguments.exchange is None:
            raise BacktestConfigurationError(
                "Corporate-action backtests require --start, --end, and --exchange."
            )
        action_store = SQLiteCorporateActionStore(
            arguments.corporate_action_root.expanduser().resolve()
        )
        expected_delisted_at: datetime | None = None
        if arguments.point_in_time_root is not None:
            point_store = SQLitePointInTimeStore(
                arguments.point_in_time_root.expanduser().resolve()
            )
            lifecycle = await point_store.get_security(
                arguments.exchange,
                arguments.symbol,
            )
            expected_delisted_at = lifecycle.delisted_at
        action_policy = CorporateActionPolicy(
            fractional_share_policy=FractionalSharePolicy(
                arguments.fractional_share_policy
            )
        )
        corporate_action_context = await action_store.build_backtest_context(
            exchange=arguments.exchange,
            initial_symbol=arguments.symbol,
            start=start,
            end=_exclusive_end(end),
            policy=action_policy,
            expected_delisted_at=expected_delisted_at,
        )
        corporate_actions = await action_store.load_context_actions(
            corporate_action_context
        )
        corporate_action_timeline = CorporateActionTimeline(
            corporate_action_context,
            corporate_actions,
        )
        dividend_tax_model = FlatRateDividendTaxModel(
            _decimal(arguments.dividend_tax_rate, "dividend tax rate")
        )
        replay_symbols = corporate_action_context.symbols

    replay = ReplayConfig(
        symbols=replay_symbols,
        interval=interval,
        start=start,
        end=end,
        include_warnings=not arguments.pass_only,
        page_size=arguments.page_size,
    )
    reader: NormalizedMarketDataReader = normalized_store
    data_context_digest: str | None = None
    point_in_time_values = (
        arguments.point_in_time_root,
        arguments.universe_id,
    )
    if any(value is not None for value in point_in_time_values):
        if not all(value is not None for value in point_in_time_values):
            raise BacktestConfigurationError(
                "Point-in-time root and universe ID are required together."
            )
        if arguments.exchange is None:
            raise BacktestConfigurationError(
                "Point-in-time backtests also require --exchange."
            )
        if start is None or end is None:
            raise BacktestConfigurationError(
                "Point-in-time backtests require explicit --start and --end values."
            )
        point_in_time_store = SQLitePointInTimeStore(
            arguments.point_in_time_root.expanduser().resolve()
        )
        exclusive_end = _exclusive_end(end)
        if corporate_action_context is None:
            context = await point_in_time_store.build_backtest_context(
                universe_id=arguments.universe_id,
                exchange=arguments.exchange,
                symbol=arguments.symbol,
                start=start,
                end=exclusive_end,
            )
            if context.delisting_id is not None:
                raise BacktestConfigurationError(
                    "Delisted-security backtests require --corporate-action-root."
                )
            validated_reader = PointInTimeValidatedCandleReader(
                normalized_store,
                point_in_time_store,
                context,
            )
            reader = validated_reader
            data_context_digest = validated_reader.context_digest
        else:
            contexts = await _build_point_in_time_symbol_contexts(
                store=point_in_time_store,
                universe_id=arguments.universe_id,
                exchange=arguments.exchange,
                start=start,
                end=exclusive_end,
                corporate_action_context=corporate_action_context,
                actions=corporate_actions,
            )
            multi_reader = PointInTimeValidatedMultiSymbolCandleReader(
                normalized_store,
                point_in_time_store,
                contexts,
            )
            reader = multi_reader
            data_context_digest = multi_reader.context_digest

    config = BacktestConfig(
        replay=replay,
        initial_symbol=arguments.symbol,
        initial_cash=_decimal(arguments.initial_cash, "initial cash"),
        commission_bps=_decimal(arguments.commission_bps, "commission bps"),
        slippage_bps=_decimal(arguments.slippage_bps, "slippage bps"),
        max_volume_participation=_decimal(
            arguments.max_volume_participation,
            "maximum volume participation",
        ),
        annualization_periods=_resolve_annualization_periods(
            interval,
            arguments.annualization_periods,
        ),
        data_context_digest=data_context_digest,
        corporate_action_context_digest=(
            None
            if corporate_action_context is None
            else corporate_action_context.context_digest
        ),
        dividend_tax_model_digest=(
            None if dividend_tax_model is None else dividend_tax_model.fingerprint
        ),
    )
    return await StrategyBacktestEngine(
        reader,
        config,
        strategy,
        corporate_action_timeline=corporate_action_timeline,
        dividend_tax_model=dividend_tax_model,
    ).run()


async def _build_point_in_time_symbol_contexts(
    *,
    store: SQLitePointInTimeStore,
    universe_id: str,
    exchange: str,
    start: datetime,
    end: datetime,
    corporate_action_context: CorporateActionBacktestContext,
    actions: tuple[CorporateActionRecord, ...],
) -> tuple[PointInTimeBacktestContext, ...]:
    segments: list[tuple[str, datetime, datetime]] = []
    current_symbol = corporate_action_context.initial_symbol
    segment_start = start
    for action in sorted(actions, key=lambda item: item.effective_at):
        if action.action_type is CorporateActionType.SYMBOL_CHANGE:
            if segment_start >= action.effective_at:
                raise BacktestConfigurationError(
                    "Symbol-change segments must have positive duration."
                )
            segments.append((current_symbol, segment_start, action.effective_at))
            assert action.new_symbol is not None
            current_symbol = action.new_symbol
            segment_start = action.effective_at
        elif action.action_type is CorporateActionType.DELISTING:
            if segment_start < action.effective_at:
                segments.append(
                    (current_symbol, segment_start, action.effective_at)
                )
            segment_start = end
            break
    if segment_start < end:
        segments.append((current_symbol, segment_start, end))
    if tuple(symbol for symbol, _, _ in segments) != (
        corporate_action_context.symbols
    ):
        raise BacktestConfigurationError(
            "Point-in-time segments do not match the corporate-action symbol path."
        )
    contexts: list[PointInTimeBacktestContext] = []
    for symbol, segment_start, segment_end in segments:
        contexts.append(
            await store.build_backtest_context(
                universe_id=universe_id,
                exchange=exchange,
                symbol=symbol,
                start=segment_start,
                end=segment_end,
            )
        )
    return tuple(contexts)


async def _run_robustness(arguments: argparse.Namespace) -> RobustnessReport:
    root = arguments.normalized_root.expanduser().resolve()
    database = root / "normalized.sqlite3"
    if not database.is_file():
        raise BacktestConfigurationError(
            f"Normalized database does not exist: {database}"
        )
    interval = CandleInterval(arguments.interval)
    plan = _walk_forward_plan_from_arguments(arguments.fold)
    strategies: tuple[RobustnessStrategySpec, ...]
    if arguments.strategy == "buy-and-hold":
        strategies = (RobustnessStrategySpec(name="buy-and-hold"),)
    else:
        strategies = sma_parameter_grid(
            base_short_window=arguments.base_short_window,
            base_long_window=arguments.base_long_window,
            short_offsets=tuple(arguments.short_offset or (0,)),
            long_offsets=tuple(arguments.long_offset or (0,)),
        )
    scenarios: tuple[StressScenario, ...] = build_stress_scenarios(
        cost_multipliers=tuple(
            _decimal(value, "cost multiplier")
            for value in (arguments.cost_multiplier or ("1",))
        ),
        execution_delays=tuple(arguments.execution_delay or (1,)),
    )
    policy = RobustnessPolicy(
        minimum_observations=arguments.minimum_observations,
        minimum_test_return=_decimal(
            arguments.minimum_test_return,
            "minimum test return",
        ),
        maximum_drawdown=_decimal(
            arguments.maximum_drawdown,
            "maximum drawdown",
        ),
        maximum_return_degradation=_decimal(
            arguments.maximum_return_degradation,
            "maximum return degradation",
        ),
        required_pass_rate=_decimal(
            arguments.required_pass_rate,
            "required pass rate",
        ),
        uptrend_threshold=_decimal(
            arguments.uptrend_threshold,
            "uptrend threshold",
        ),
        downtrend_threshold=_decimal(
            arguments.downtrend_threshold,
            "downtrend threshold",
        ),
        high_volatility_threshold=arguments.high_volatility_threshold,
    )
    base_config = BacktestConfig(
        replay=ReplayConfig(
            symbols=(arguments.symbol,),
            interval=interval,
            include_warnings=not arguments.pass_only,
            page_size=arguments.page_size,
        ),
        initial_cash=_decimal(arguments.initial_cash, "initial cash"),
        commission_bps=_decimal(arguments.commission_bps, "commission bps"),
        slippage_bps=_decimal(arguments.slippage_bps, "slippage bps"),
        max_volume_participation=_decimal(
            arguments.max_volume_participation,
            "maximum volume participation",
        ),
        annualization_periods=_resolve_annualization_periods(
            interval,
            arguments.annualization_periods,
        ),
        historical_dataset_digest=arguments.historical_dataset_digest,
    )
    reader = SQLiteNormalizedMarketDataStore(root)
    return await DeterministicRobustnessRunner(
        run_factory=StandardBacktestRunFactory(reader),
        base_config=base_config,
        plan=plan,
        strategies=strategies,
        scenarios=scenarios,
        policy=policy,
    ).run()


def _walk_forward_plan_from_arguments(values: list[str]) -> WalkForwardPlan:
    folds: list[WalkForwardFold] = []
    for fold_number, value in enumerate(values, start=1):
        parts = tuple(part.strip() for part in value.split(","))
        if len(parts) != 6 or any(not part for part in parts):
            raise BacktestConfigurationError(
                "Each --fold must contain six comma-separated ISO timestamps."
            )
        boundaries = tuple(_parse_research_boundary(part) for part in parts)
        folds.append(
            WalkForwardFold(
                fold_number=fold_number,
                train=ResearchWindow(boundaries[0], boundaries[1]),
                validation=ResearchWindow(boundaries[2], boundaries[3]),
                test=ResearchWindow(boundaries[4], boundaries[5]),
            )
        )
    return WalkForwardPlan(tuple(folds))


async def _run_dataset(arguments: argparse.Namespace) -> str:
    store = SQLiteHistoricalDatasetStore(arguments.root.expanduser().resolve())
    command = arguments.dataset_command
    if command == "import":
        holidays = tuple(
            sorted(date.fromisoformat(value) for value in arguments.holiday)
        )
        spec = HistoricalDatasetImportSpec(
            provider=arguments.provider,
            exchange=arguments.exchange.upper(),
            symbol=arguments.symbol.upper(),
            interval=CandleInterval(arguments.interval),
            currency=arguments.currency.upper(),
            code_commit=arguments.code_commit,
            point_in_time_context_digest=arguments.point_in_time_context_digest,
            corporate_action_context_digest=(
                arguments.corporate_action_context_digest
            ),
            policy=HistoricalDatasetPolicy(
                timezone=arguments.timezone,
                missing_session_policy=MissingSessionPolicy(
                    arguments.missing_session_policy
                ),
                holidays=holidays,
            ),
        )
        snapshot = await HistoricalDatasetImporter(store).import_csv(
            arguments.source_file,
            spec,
        )
        return _format_dataset_snapshot(snapshot)
    if command == "validate":
        return _format_dataset_snapshot(await store.verify(arguments.dataset_id))
    if command == "freeze":
        return _format_dataset_snapshot(await store.freeze(arguments.dataset_id))
    if command == "inspect":
        return _format_dataset_snapshot(
            await store.get_snapshot(arguments.dataset_id)
        )
    if command == "link-benchmark":
        link = await store.link_benchmark(
            arguments.dataset_id,
            arguments.benchmark_dataset_id,
        )
        return "\n".join(
            (
                f"Benchmark link ID: {link.link_id}",
                f"Dataset ID:        {link.dataset_id}",
                f"Benchmark ID:      {link.benchmark_dataset_id}",
                f"Alignment digest:  {link.alignment_digest}",
            )
        )
    if command == "list":
        snapshots = await store.list_snapshots(limit=arguments.limit)
        if not snapshots:
            return "No historical datasets found."
        return "\n".join(
            f"{snapshot.manifest.dataset_id} | {snapshot.state.value} | "
            f"{snapshot.manifest.symbols[0]} | {snapshot.manifest.item_count} items"
            for snapshot in snapshots
        )
    _unreachable()


async def _run_corporate_actions(arguments: argparse.Namespace) -> str:
    store = SQLiteCorporateActionStore(arguments.root.expanduser().resolve())
    command = arguments.corporate_action_command
    if command == "register":
        action = CorporateActionRecord(
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            action_type=CorporateActionType(arguments.type),
            effective_at=_parse_research_boundary(arguments.effective_at),
            available_at=_parse_research_boundary(arguments.available_at),
            source=arguments.source,
            source_digest=arguments.source_digest,
            ratio_numerator=arguments.ratio_numerator,
            ratio_denominator=arguments.ratio_denominator,
            cash_amount_per_share=_optional_decimal_argument(
                arguments.cash_amount_per_share,
                "cash amount per share",
            ),
            declared_at=_optional_research_boundary(arguments.declared_at),
            ex_at=_optional_research_boundary(arguments.ex_at),
            record_at=_optional_research_boundary(arguments.record_at),
            payment_at=_optional_research_boundary(arguments.payment_at),
            new_symbol=arguments.new_symbol,
            cash_in_lieu_price=_optional_decimal_argument(
                arguments.cash_in_lieu_price,
                "cash-in-lieu price",
            ),
            delisting_cash_price=_optional_decimal_argument(
                arguments.delisting_cash_price,
                "delisting cash price",
            ),
            delisting_recovery_rate=_optional_decimal_argument(
                arguments.delisting_recovery_rate,
                "delisting recovery rate",
            ),
        )
        return _format_corporate_action(await store.save_action(action))
    if command == "inspect":
        return _format_corporate_action(
            await store.get_action(arguments.action_id)
        )
    if command == "list":
        actions = await store.list_actions(
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            start=_optional_research_boundary(arguments.start),
            end=_optional_research_boundary(arguments.end),
        )
        return _format_corporate_action_list(actions)
    if command == "backtest-context":
        context = await store.build_backtest_context(
            exchange=arguments.exchange,
            initial_symbol=arguments.symbol,
            start=_parse_research_boundary(arguments.start),
            end=_parse_research_boundary(arguments.end),
            policy=CorporateActionPolicy(
                fractional_share_policy=FractionalSharePolicy(
                    arguments.fractional_share_policy
                )
            ),
            expected_delisted_at=_optional_research_boundary(
                arguments.expected_delisted_at
            ),
        )
        return _format_corporate_action_context(context)
    _unreachable()


async def _run_research(arguments: argparse.Namespace) -> str:
    root = arguments.root.expanduser().resolve()
    registry = SQLiteExperimentRegistry(root)
    if arguments.research_command == "register":
        split = ResearchSplit(
            train=ResearchWindow(
                _parse_research_boundary(arguments.train_start),
                _parse_research_boundary(arguments.train_end),
            ),
            validation=ResearchWindow(
                _parse_research_boundary(arguments.validation_start),
                _parse_research_boundary(arguments.validation_end),
            ),
            holdout=ResearchWindow(
                _parse_research_boundary(arguments.holdout_start),
                _parse_research_boundary(arguments.holdout_end),
            ),
        )
        spec = ExperimentSpec(
            strategy_name=arguments.strategy,
            strategy_version=arguments.strategy_version,
            parameters_json=_canonical_json_argument(
                arguments.parameters_json,
                "parameters",
            ),
            dataset_digest=arguments.dataset_digest,
            code_commit=arguments.code_commit,
            cost_model_json=_canonical_json_argument(
                arguments.cost_model_json,
                "cost model",
            ),
            execution_model_json=_canonical_json_argument(
                arguments.execution_model_json,
                "execution model",
            ),
            split=split,
            search_audit=ParameterSearchAudit(
                search_id=arguments.search_id,
                search_space_json=_canonical_json_argument(
                    arguments.search_space_json,
                    "search space",
                ),
                trial_number=arguments.trial_number,
                total_trials=arguments.total_trials,
                selection_metric=arguments.selection_metric,
            ),
            parent_experiment_id=arguments.parent_experiment_id,
            change_reason=arguments.change_reason,
            point_in_time_context_digest=(
                arguments.point_in_time_context_digest
            ),
            point_in_time_policy_json=(
                None
                if arguments.point_in_time_policy_json is None
                else _canonical_json_argument(
                    arguments.point_in_time_policy_json,
                    "point-in-time policy",
                )
            ),
            corporate_action_context_digest=(
                arguments.corporate_action_context_digest
            ),
            corporate_action_policy_json=(
                None
                if arguments.corporate_action_policy_json is None
                else _canonical_json_argument(
                    arguments.corporate_action_policy_json,
                    "corporate-action policy",
                )
            ),
            dividend_tax_model_digest=arguments.dividend_tax_model_digest,
        )
        record = await registry.register(spec)
        return _format_research_registration(record)
    if arguments.research_command == "inspect":
        snapshot = await registry.get(arguments.experiment_id)
        return _format_research_snapshot(snapshot)
    if arguments.research_command == "record-outcome":
        outcome = ExperimentOutcome(
            experiment_id=arguments.experiment_id,
            status=ExperimentStatus(arguments.status),
            completed_at=datetime.now(UTC),
            result_digest=arguments.result_digest,
            failure_reason=arguments.failure_reason,
        )
        stored = await registry.record_outcome(outcome)
        return _format_research_outcome(stored)
    if arguments.research_command == "consume-holdout":
        consumption = HoldoutConsumption(
            experiment_id=arguments.experiment_id,
            result_digest=arguments.result_digest,
            consumed_at=datetime.now(UTC),
        )
        stored_consumption = await registry.consume_holdout(consumption)
        return _format_holdout_consumption(stored_consumption)
    _unreachable()


async def _run_point_in_time(arguments: argparse.Namespace) -> str:
    root = arguments.root.expanduser().resolve()
    store = SQLitePointInTimeStore(root)
    command = arguments.point_in_time_command
    if command == "register-security":
        lifecycle = SecurityLifecycle(
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            listed_at=_parse_research_boundary(arguments.listed_at),
            tradable_from=_parse_research_boundary(arguments.tradable_from),
            delisted_at=_optional_research_boundary(arguments.delisted_at),
            tradable_until=_optional_research_boundary(arguments.tradable_until),
            source=arguments.source,
            source_digest=arguments.source_digest,
        )
        stored = await store.save_security(lifecycle)
        return _format_security_lifecycle(stored)
    if command == "register-membership":
        membership = UniverseMembership(
            universe_id=arguments.universe_id,
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            member_from=_parse_research_boundary(arguments.member_from),
            member_until=_optional_research_boundary(arguments.member_until),
            available_at=_parse_research_boundary(arguments.available_at),
            source=arguments.source,
            source_digest=arguments.source_digest,
        )
        stored_membership = await store.save_membership(membership)
        return _format_universe_membership(stored_membership)
    if command == "register-availability":
        availability = DataAvailabilityRecord(
            data_id=arguments.data_id,
            data_kind=PointInTimeDataKind(arguments.data_kind),
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            effective_at=_parse_research_boundary(arguments.effective_at),
            available_at=_parse_research_boundary(arguments.available_at),
            source=arguments.source,
            source_digest=arguments.source_digest,
        )
        stored_availability = await store.save_availability(availability)
        return _format_data_availability(stored_availability)
    if command == "register-delisting":
        delisting = DelistingRecord(
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            last_tradable_at=_parse_research_boundary(
                arguments.last_tradable_at
            ),
            delisted_at=_parse_research_boundary(arguments.delisted_at),
            available_at=_parse_research_boundary(arguments.available_at),
            reason=DelistingReason(arguments.reason),
            source=arguments.source,
            source_digest=arguments.source_digest,
        )
        stored_delisting = await store.save_delisting(delisting)
        return _format_delisting(stored_delisting)
    if command == "snapshot":
        snapshot = await store.build_snapshot(
            arguments.universe_id,
            _parse_research_boundary(arguments.as_of),
        )
        return _format_point_in_time_snapshot(snapshot)
    if command == "backtest-context":
        start = _parse_research_boundary(arguments.start)
        end = _parse_research_boundary(arguments.end)
        context = await store.build_backtest_context(
            universe_id=arguments.universe_id,
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            start=start,
            end=end,
        )
        return _format_point_in_time_context(context)
    if command == "inspect-security":
        lifecycle = await store.get_security(arguments.exchange, arguments.symbol)
        return _format_security_lifecycle(lifecycle)
    if command == "validate-access":
        grant = await store.validate_access(
            universe_id=arguments.universe_id,
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            data_id=arguments.data_id,
            event_at=_parse_research_boundary(arguments.event_at),
            decision_at=_parse_research_boundary(arguments.decision_at),
        )
        return _format_access_grant(grant)
    _unreachable()


def _parse_research_boundary(value: str) -> datetime:
    try:
        if len(value) == 10:
            return datetime.combine(
                date.fromisoformat(value),
                time.min,
                tzinfo=UTC,
            )
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid research timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "Research timestamps must include a timezone or use YYYY-MM-DD."
        )
    return parsed.astimezone(UTC)


def _optional_research_boundary(value: str | None) -> datetime | None:
    return None if value is None else _parse_research_boundary(value)


def _exclusive_end(value: datetime) -> datetime:
    try:
        return value + timedelta(microseconds=1)
    except OverflowError as error:
        raise BacktestConfigurationError(
            "Point-in-time end timestamp is too large."
        ) from error


def _canonical_json_argument(value: str, field_name: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON for {field_name}.") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name.capitalize()} JSON must be an object.")
    return canonical_json_object(parsed)


def _format_dataset_snapshot(snapshot: HistoricalDatasetSnapshot) -> str:
    manifest = snapshot.manifest
    issue_count = len(manifest.issues)
    return "\n".join(
        (
            f"Dataset ID:          {manifest.dataset_id}",
            f"State:               {snapshot.state.value}",
            f"Dataset digest:      {snapshot.dataset_digest}",
            f"Source SHA-256:      {manifest.source_sha256}",
            f"Normalized digest:  {manifest.normalized_digest}",
            f"Provider:             {manifest.provider}",
            f"Exchange:             {manifest.exchange}",
            f"Symbol:               {manifest.symbols[0]}",
            f"Interval:             {manifest.interval.value}",
            f"Timezone:             {manifest.timezone}",
            f"Items:                {manifest.item_count}",
            f"Quality status:       {manifest.quality_status.value}",
            f"Integrity warnings:   {issue_count}",
            "Network access:       DISABLED",
        )
    )


def _format_research_registration(record: ExperimentRecord) -> str:
    return "\n".join(
        (
            "Execution mode: RESEARCH_REGISTRY",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Experiment ID:       {record.experiment_id}",
            f"Research digest:     {record.research_digest}",
            f"Strategy:            {record.spec.strategy_name}",
            f"Trial:               {record.spec.search_audit.trial_number}/"
            f"{record.spec.search_audit.total_trials}",
            f"Registered at:       {record.registered_at.isoformat()}",
            "Point-in-time:       "
            f"{record.spec.point_in_time_context_digest or 'NOT_RECORDED'}",
            "Holdout state:       UNTOUCHED",
        )
    )


def _format_research_snapshot(snapshot: ExperimentSnapshot) -> str:
    outcome = (
        "not_recorded"
        if snapshot.outcome is None
        else snapshot.outcome.status.value
    )
    holdout = (
        "UNTOUCHED"
        if snapshot.holdout_consumption is None
        else "CONSUMED"
    )
    lines = [
        "Execution mode: RESEARCH_REGISTRY",
        "Network access: DISABLED",
        "Live trading: DISABLED",
        "Order submission: DISABLED",
        "",
        f"Experiment ID:       {snapshot.record.experiment_id}",
        f"Research digest:     {snapshot.record.research_digest}",
        f"Strategy:            {snapshot.record.spec.strategy_name}",
        "Point-in-time:       "
        f"{snapshot.record.spec.point_in_time_context_digest or 'NOT_RECORDED'}",
        f"Status:              {outcome}",
        f"Holdout state:       {holdout}",
    ]
    if snapshot.outcome is not None:
        lines.append(
            "Result digest:       "
            f"{snapshot.outcome.result_digest or 'N/A'}"
        )
        lines.append(
            "Failure reason:      "
            f"{snapshot.outcome.failure_reason or 'N/A'}"
        )
    if snapshot.holdout_consumption is not None:
        lines.append(
            "Holdout digest:      "
            f"{snapshot.holdout_consumption.result_digest}"
        )
        lines.append(
            "Holdout consumed:    "
            f"{snapshot.holdout_consumption.consumed_at.isoformat()}"
        )
    return "\n".join(lines)


def _format_research_outcome(outcome: ExperimentOutcome) -> str:
    return "\n".join(
        (
            "Execution mode: RESEARCH_REGISTRY",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Experiment ID:       {outcome.experiment_id}",
            f"Status:              {outcome.status.value}",
            f"Result digest:       {outcome.result_digest or 'N/A'}",
            f"Failure reason:      {outcome.failure_reason or 'N/A'}",
        )
    )


def _format_holdout_consumption(consumption: HoldoutConsumption) -> str:
    return "\n".join(
        (
            "Execution mode: RESEARCH_REGISTRY",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Experiment ID:       {consumption.experiment_id}",
            "Holdout state:       CONSUMED",
            f"Holdout digest:      {consumption.result_digest}",
            f"Consumed at:         {consumption.consumed_at.isoformat()}",
        )
    )

def _format_robustness_report(report: RobustnessReport) -> str:
    return "\n".join(
        (
            "Execution mode: ROBUSTNESS_RESEARCH",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Report ID:           {report.report_id}",
            f"Walk-forward folds:  {len(report.plan.folds)}",
            f"Robustness cases:    {len(report.cases)}",
            f"Median test return:  {report.median_test_return:.4%}",
            f"Worst test return:   {report.worst_test_return:.4%}",
            "Median OOS change:   "
            f"{report.median_return_degradation:.4%}",
            f"Worst drawdown:      {report.worst_maximum_drawdown:.4%}",
            f"Pass rate:           {report.pass_rate:.2%}",
            f"Policy result:       {'PASS' if report.passed else 'FAIL'}",
            f"Report digest:       {report.report_digest}",
        )
    )


def _format_security_lifecycle(lifecycle: SecurityLifecycle) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Security:            {lifecycle.exchange}:{lifecycle.symbol}",
            f"Lifecycle ID:        {lifecycle.lifecycle_id}",
            f"Listed at:           {lifecycle.listed_at.isoformat()}",
            f"Tradable from:       {lifecycle.tradable_from.isoformat()}",
            "Tradable until:      "
            f"{_optional_iso_output(lifecycle.tradable_until)}",
            f"Delisted at:         {_optional_iso_output(lifecycle.delisted_at)}",
        )
    )


def _format_universe_membership(membership: UniverseMembership) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Universe:            {membership.universe_id}",
            f"Security:            {membership.exchange}:{membership.symbol}",
            f"Membership ID:       {membership.membership_id}",
            f"Effective from:      {membership.member_from.isoformat()}",
            "Effective until:     "
            f"{_optional_iso_output(membership.member_until)}",
            f"Available at:        {membership.available_at.isoformat()}",
        )
    )


def _format_data_availability(availability: DataAvailabilityRecord) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Data ID:             {availability.data_id}",
            f"Availability ID:     {availability.availability_id}",
            f"Data kind:           {availability.data_kind.value}",
            f"Security:            {availability.exchange}:{availability.symbol}",
            f"Effective at:        {availability.effective_at.isoformat()}",
            f"Available at:        {availability.available_at.isoformat()}",
        )
    )


def _format_delisting(delisting: DelistingRecord) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Security:            {delisting.exchange}:{delisting.symbol}",
            f"Delisting ID:        {delisting.delisting_id}",
            f"Last tradable:       {delisting.last_tradable_at.isoformat()}",
            f"Delisted at:         {delisting.delisted_at.isoformat()}",
            f"Reason:              {delisting.reason.value}",
        )
    )


def _format_point_in_time_snapshot(snapshot: PointInTimeSnapshot) -> str:
    members = ", ".join(
        f"{member.exchange}:{member.symbol}" for member in snapshot.members
    )
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Universe:            {snapshot.universe_id}",
            f"As of:               {snapshot.as_of.isoformat()}",
            f"Members:             {len(snapshot.members)}",
            f"Member securities:   {members}",
            f"Universe digest:     {snapshot.universe_digest}",
            f"Snapshot digest:     {snapshot.snapshot_digest}",
        )
    )


def _format_point_in_time_context(
    context: PointInTimeBacktestContext,
) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Universe:            {context.universe_id}",
            f"Security:            {context.exchange}:{context.symbol}",
            f"Period:              {context.start.isoformat()} -> "
            f"{context.end.isoformat()}",
            f"Membership records:  {len(context.membership_ids)}",
            f"Availability records:{len(context.availability_ids):>6}",
            f"Policy JSON:         {point_in_time_policy_json(context.policy)}",
            f"Context digest:      {context.context_digest}",
        )
    )


def _format_access_grant(grant: PointInTimeAccessGrant) -> str:
    return "\n".join(
        (
            "Execution mode: POINT_IN_TIME_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Data ID:             {grant.data_id}",
            f"Security:            {grant.exchange}:{grant.symbol}",
            f"Decision at:         {grant.decision_at.isoformat()}",
            "Eligibility:         GRANTED",
            f"Grant digest:        {grant.grant_digest}",
        )
    )


def _format_corporate_action(action: CorporateActionRecord) -> str:
    details = json.dumps(
        action.to_document(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "\n".join(
        (
            "Execution mode: CORPORATE_ACTION_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Action ID:           {action.action_id}",
            f"Security:            {action.exchange}:{action.symbol}",
            f"Action type:         {action.action_type.value}",
            f"Effective at:        {action.effective_at.isoformat()}",
            f"Available at:        {action.available_at.isoformat()}",
            f"Action JSON:         {details}",
        )
    )


def _format_corporate_action_list(
    actions: tuple[CorporateActionRecord, ...],
) -> str:
    action_lines = tuple(
        f"{action.effective_at.isoformat()} | {action.action_type.value} | "
        f"{action.symbol} | {action.action_id}"
        for action in actions
    )
    return "\n".join(
        (
            "Execution mode: CORPORATE_ACTION_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Actions:              {len(actions)}",
            *(action_lines or ("No matching actions.",)),
        )
    )


def _format_corporate_action_context(
    context: CorporateActionBacktestContext,
) -> str:
    return "\n".join(
        (
            "Execution mode: CORPORATE_ACTION_CATALOG",
            "Network access: DISABLED",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Security path:       {' -> '.join(context.symbols)}",
            f"Period:              {context.start.isoformat()} -> "
            f"{context.end.isoformat()}",
            f"Actions:             {len(context.action_ids)}",
            f"Dataset digest:      {context.action_dataset_digest}",
            f"Policy JSON:         {corporate_action_policy_json(context.policy)}",
            f"Context digest:      {context.context_digest}",
        )
    )


def _optional_iso_output(value: datetime | None) -> str:
    return "N/A" if value is None else value.isoformat()


def _strategy_from_arguments(arguments: argparse.Namespace) -> Strategy:
    if arguments.strategy == "buy-and-hold":
        return BuyAndHoldStrategy()
    return SmaCrossoverStrategy(
        short_window=arguments.short_window,
        long_window=arguments.long_window,
    )


def _resolve_annualization_periods(
    interval: CandleInterval,
    configured_value: int | None,
) -> int:
    if configured_value is not None:
        return configured_value
    if interval is CandleInterval.DAY_1:
        return 252
    raise BacktestConfigurationError(
        "Intraday backtests require --annualization-periods because market "
        "session lengths differ by venue."
    )


def _parse_time(value: str | None, *, is_end: bool) -> datetime | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            boundary = time.max if is_end else time.min
            return datetime.combine(parsed_date, boundary, tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BacktestConfigurationError(
            f"Invalid ISO-8601 timestamp: {value}"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BacktestConfigurationError(
            "Backtest timestamps must include a timezone or use YYYY-MM-DD."
        )
    return parsed.astimezone(UTC)


def _optional_decimal_argument(
    value: str | None,
    field_name: str,
) -> Decimal | None:
    return None if value is None else _decimal(value, field_name)


def _decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise BacktestConfigurationError(
            f"Invalid decimal value for {field_name}: {value}"
        ) from error
    if not parsed.is_finite():
        raise BacktestConfigurationError(
            f"{field_name.capitalize()} must be finite."
        )
    return parsed


def _format_result(result: BacktestRunResult) -> str:
    metrics = result.metrics
    benchmark = (
        "N/A"
        if metrics.benchmark_return is None
        else _percent(metrics.benchmark_return)
    )
    replay = result.config.replay
    period_start = "unbounded" if replay.start is None else replay.start.isoformat()
    period_end = "unbounded" if replay.end is None else replay.end.isoformat()
    observed_start = (
        "N/A"
        if result.replay_result.first_event_at is None
        else result.replay_result.first_event_at.isoformat()
    )
    observed_end = (
        "N/A"
        if result.replay_result.last_event_at is None
        else result.replay_result.last_event_at.isoformat()
    )
    final_snapshot = None if not result.equity_curve else result.equity_curve[-1]
    final_quantity = 0 if final_snapshot is None else final_snapshot.quantity
    filled_orders = sum(
        record.status is OrderStatus.FILLED for record in result.orders
    )
    partial_orders = sum(
        record.status is OrderStatus.PARTIALLY_FILLED for record in result.orders
    )
    rejected_orders = sum(
        record.status is OrderStatus.REJECTED for record in result.orders
    )
    expired_orders = sum(
        record.status is OrderStatus.EXPIRED for record in result.orders
    )
    dividend_gross = (
        Decimal("0")
        if final_snapshot is None
        else final_snapshot.total_dividend_gross
    )
    dividend_tax = (
        Decimal("0")
        if final_snapshot is None
        else final_snapshot.total_dividend_tax
    )
    dividend_net = (
        Decimal("0")
        if final_snapshot is None
        else final_snapshot.total_dividend_net
    )
    cash_in_lieu = (
        Decimal("0")
        if final_snapshot is None
        else final_snapshot.total_cash_in_lieu
    )
    lines = [
        "Execution mode: REPLAY",
        "Network access: DISABLED",
        "Live trading: DISABLED",
        "Order submission: DISABLED",
        "",
        f"Strategy:            {result.strategy.name}",
        f"Symbol:              {result.config.symbol}",
        f"Interval:            {replay.interval.value}",
        f"Requested period:    {period_start} -> {period_end}",
        f"Observed period:     {observed_start} -> {observed_end}",
        f"Events:              {result.replay_result.event_count}",
        f"PASS / WARNING:      {result.replay_result.pass_count} / "
        f"{result.replay_result.warning_count}",
        f"Commission (bps):    {result.config.commission_bps}",
        f"Slippage (bps):      {result.config.slippage_bps}",
        f"Initial equity:      {metrics.initial_equity:,.2f}",
        f"Final equity:        {metrics.final_equity:,.2f}",
        f"Total return:        {_percent(metrics.total_return)}",
        f"Benchmark return:    {benchmark}",
        f"Excess return:       {_optional_decimal_percent(metrics.excess_return)}",
        f"Maximum drawdown:    {_percent(metrics.maximum_drawdown)}",
        f"CAGR:                {_optional_percent(metrics.cagr)}",
        f"Annual volatility:   {_optional_percent(metrics.annualized_volatility)}",
        f"Sharpe ratio:        {_optional_number(metrics.sharpe_ratio)}",
        f"Sortino ratio:       {_optional_number(metrics.sortino_ratio)}",
        f"Calmar ratio:        {_optional_number(metrics.calmar_ratio)}",
        f"Orders:              {len(result.orders)}",
        f"Filled / partial:    {filled_orders} / {partial_orders}",
        f"Rejected / expired:  {rejected_orders} / {expired_orders}",
        f"Closed trades:       {metrics.trade_count}",
        f"Win rate:            {_optional_decimal_percent(metrics.win_rate)}",
        f"Profit factor:       {_optional_decimal_number(metrics.profit_factor)}",
        f"Average win:         {_optional_decimal_number(metrics.average_win)}",
        f"Average loss:        {_optional_decimal_number(metrics.average_loss)}",
        f"Turnover:            {metrics.turnover:.4f}",
        f"Average exposure:    {_percent(metrics.average_exposure)}",
        f"Final quantity:      {final_quantity}",
        f"Commission cost:     {metrics.commission_cost:,.2f}",
        f"Slippage cost:       {metrics.slippage_cost:,.2f}",
        f"Corporate actions:   {len(result.corporate_actions)}",
        f"Dividend gross:      {dividend_gross:,.2f}",
        f"Dividend tax:        {dividend_tax:,.2f}",
        f"Dividend net:        {dividend_net:,.2f}",
        f"Cash in lieu:        {cash_in_lieu:,.2f}",
        f"Replay digest:       {result.replay_result.event_digest}",
        "Data context:        "
        f"{result.config.data_context_digest or 'NOT_VALIDATED'}",
        "Corporate context:   "
        f"{result.config.corporate_action_context_digest or 'NOT_APPLIED'}",
        "Dividend tax model:  "
        f"{result.config.dividend_tax_model_digest or 'NOT_APPLIED'}",
        f"Config fingerprint:  {result.config_fingerprint}",
        f"Run digest:          {result.run_digest}",
    ]
    return "\n".join(lines)


def _percent(value: Decimal) -> str:
    return f"{value * Decimal('100'):.2f}%"


def _optional_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _optional_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _optional_decimal_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def _optional_decimal_number(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:,.4f}"


def _unreachable() -> NoReturn:
    raise AssertionError("Unreachable CLI command state.")


if __name__ == "__main__":
    main(sys.argv[1:])
