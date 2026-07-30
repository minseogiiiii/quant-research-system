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
    DataAvailabilityRecord,
    DelistingReason,
    DelistingRecord,
    ExperimentOutcome,
    ExperimentRecord,
    ExperimentSnapshot,
    ExperimentSpec,
    ExperimentStatus,
    HoldoutConsumption,
    ParameterSearchAudit,
    PointInTimeAccessGrant,
    PointInTimeBacktestContext,
    PointInTimeDataKind,
    PointInTimeSnapshot,
    PointInTimeValidatedCandleReader,
    ResearchError,
    ResearchSplit,
    ResearchWindow,
    SecurityLifecycle,
    SQLiteExperimentRegistry,
    SQLitePointInTimeStore,
    UniverseMembership,
    canonical_json_object,
    point_in_time_policy_json,
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
    replay = ReplayConfig(
        symbols=(arguments.symbol,),
        interval=interval,
        start=start,
        end=end,
        include_warnings=not arguments.pass_only,
        page_size=arguments.page_size,
    )
    normalized_store = SQLiteNormalizedMarketDataStore(root)
    reader: NormalizedMarketDataReader = normalized_store
    data_context_digest: str | None = None
    point_in_time_values = (
        arguments.point_in_time_root,
        arguments.universe_id,
        arguments.exchange,
    )
    if any(value is not None for value in point_in_time_values):
        if not all(value is not None for value in point_in_time_values):
            raise BacktestConfigurationError(
                "Point-in-time root, universe ID, and exchange are required together."
            )
        if start is None or end is None:
            raise BacktestConfigurationError(
                "Point-in-time backtests require explicit --start and --end values."
            )
        point_in_time_store = SQLitePointInTimeStore(
            arguments.point_in_time_root.expanduser().resolve()
        )
        context = await point_in_time_store.build_backtest_context(
            universe_id=arguments.universe_id,
            exchange=arguments.exchange,
            symbol=arguments.symbol,
            start=start,
            end=_exclusive_end(end),
        )
        reader = PointInTimeValidatedCandleReader(
            normalized_store,
            point_in_time_store,
            context,
        )
        data_context_digest = context.context_digest
    config = BacktestConfig(
        replay=replay,
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
    )
    return await StrategyBacktestEngine(reader, config, strategy).run()


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
    final_quantity = 0 if not result.equity_curve else result.equity_curve[-1].quantity
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
        f"Replay digest:       {result.replay_result.event_digest}",
        "Data context:        "
        f"{result.config.data_context_digest or 'NOT_VALIDATED'}",
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
