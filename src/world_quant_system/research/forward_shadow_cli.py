from __future__ import annotations

import argparse
from pathlib import Path

from world_quant_system.research.forward_shadow import (
    DeterministicForwardShadowRunner,
    FixedForwardShadowClock,
    SystemForwardShadowClock,
    decimal_argument,
    load_forward_shadow_observations,
)
from world_quant_system.research.forward_shadow_models import (
    ForwardShadowBatchReport,
    ForwardShadowError,
    ForwardShadowPolicy,
)
from world_quant_system.research.forward_shadow_reporting import (
    AtomicJsonForwardShadowReportWriter,
    AtomicJsonForwardShadowStateStore,
)
from world_quant_system.research.historical_shadow import (
    load_shadow_evidence_manifest,
)
from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowError,
    parse_utc_datetime,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-forward-shadow",
        description=(
            "Advance a crash-safe forward-shadow checkpoint from an append-only "
            "observation batch without network, broker, or order submission."
        ),
    )
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--observations-jsonl", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument(
        "--allocation",
        choices=tuple(item.value for item in AllocationMethod),
        default=AllocationMethod.EQUAL_WEIGHT.value,
    )
    parser.add_argument("--initial-equity", default="1000000")
    parser.add_argument(
        "--rebalance-every-observations",
        type=int,
        default=5,
    )
    parser.add_argument("--volatility-lookback", type=int, default=20)
    parser.add_argument(
        "--execution-delay-observations",
        type=int,
        default=1,
    )
    parser.add_argument("--minimum-active-candidates", type=int, default=2)
    parser.add_argument("--minimum-cash-weight", default="0.05")
    parser.add_argument("--maximum-candidate-weight", default="0.40")
    parser.add_argument("--maximum-one-way-turnover", default="1")
    parser.add_argument("--transaction-cost-bps", default="10")
    parser.add_argument("--maximum-evidence-age-days", type=int, default=120)
    parser.add_argument(
        "--maximum-observation-lateness-seconds",
        type=int,
        default=900,
    )
    parser.add_argument(
        "--maximum-future-clock-skew-seconds",
        type=int,
        default=5,
    )
    parser.add_argument("--maximum-drawdown-before-halt", default="-0.30")
    parser.add_argument(
        "--now",
        help="Optional deterministic ISO-8601 clock value for testing.",
    )
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    state_store = AtomicJsonForwardShadowStateStore(arguments.state_file)
    try:
        clock = (
            SystemForwardShadowClock()
            if arguments.now is None
            else FixedForwardShadowClock(
                parse_utc_datetime(arguments.now, "now")
            )
        )
        report = DeterministicForwardShadowRunner(
            evidence_manifest=load_shadow_evidence_manifest(
                arguments.evidence_manifest
            ),
            policy=_policy(arguments),
            clock=clock,
        ).process(
            load_forward_shadow_observations(arguments.observations_jsonl),
            state=state_store.load(),
        )
        state_store.save(report.state)
        print(format_forward_shadow_report(report))
        if arguments.json_output is not None:
            AtomicJsonForwardShadowReportWriter(arguments.json_output).write(
                report
            )
    except (ForwardShadowError, HistoricalShadowError) as error:
        parser.error(str(error))


def format_forward_shadow_report(report: ForwardShadowBatchReport) -> str:
    state = report.state
    last_sequence = "none" if state.last_sequence is None else state.last_sequence
    return "\n".join(
        (
            "Execution mode: FORWARD_SHADOW",
            "Network access: DISABLED",
            "Broker provider: NONE",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Report ID:                    {report.report_id}",
            f"State digest:                 {state.state_digest}",
            f"Processed observations:       {report.processed_observations}",
            f"Duplicate observations:       {report.duplicate_observations}",
            f"Last sequence:                {last_sequence}",
            f"Current equity:               {state.equity}",
            f"Cash weight:                  {state.cash_weight}",
            f"Active candidates:            {len(state.active_weights)}",
            f"Pending allocation:           {state.pending_allocation is not None}",
            f"Feed stale:                   {state.feed_stale}",
            f"Risk halted:                  {state.risk_halted}",
            f"Decision:                     {state.decision.value}",
            f"Journal entries:              {len(state.journal)}",
            f"Report digest:                {report.report_digest}",
        )
    )


def _policy(arguments: argparse.Namespace) -> ForwardShadowPolicy:
    return ForwardShadowPolicy(
        initial_equity=decimal_argument(
            arguments.initial_equity,
            "initial equity",
        ),
        allocation_method=AllocationMethod(arguments.allocation),
        rebalance_every_observations=(
            arguments.rebalance_every_observations
        ),
        volatility_lookback=arguments.volatility_lookback,
        execution_delay_observations=(
            arguments.execution_delay_observations
        ),
        minimum_active_candidates=arguments.minimum_active_candidates,
        minimum_cash_weight=decimal_argument(
            arguments.minimum_cash_weight,
            "minimum cash weight",
        ),
        maximum_candidate_weight=decimal_argument(
            arguments.maximum_candidate_weight,
            "maximum candidate weight",
        ),
        maximum_one_way_turnover=decimal_argument(
            arguments.maximum_one_way_turnover,
            "maximum one-way turnover",
        ),
        transaction_cost_bps=decimal_argument(
            arguments.transaction_cost_bps,
            "transaction cost bps",
        ),
        maximum_evidence_age_days=arguments.maximum_evidence_age_days,
        maximum_observation_lateness_seconds=(
            arguments.maximum_observation_lateness_seconds
        ),
        maximum_future_clock_skew_seconds=(
            arguments.maximum_future_clock_skew_seconds
        ),
        maximum_drawdown_before_halt=decimal_argument(
            arguments.maximum_drawdown_before_halt,
            "maximum drawdown before halt",
        ),
    )


if __name__ == "__main__":
    main()
