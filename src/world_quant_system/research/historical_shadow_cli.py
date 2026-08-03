from __future__ import annotations

import argparse
from pathlib import Path

from world_quant_system.research.historical_shadow import (
    DeterministicHistoricalShadowRunner,
    decimal_argument,
    load_shadow_evidence_manifest,
)
from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowError,
    HistoricalShadowPolicy,
    HistoricalShadowReport,
    ShadowScenarioName,
)
from world_quant_system.research.historical_shadow_reporting import (
    AtomicJsonHistoricalShadowReportWriter,
)
from world_quant_system.research.portfolio_promotion import (
    parse_candidate_returns_csv,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    PortfolioPromotionError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-shadow",
        description=(
            "Run deterministic, point-in-time historical shadow portfolio "
            "simulation without network, broker, or order submission."
        ),
    )
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--returns-csv", type=Path, required=True)
    parser.add_argument(
        "--allocation",
        choices=tuple(item.value for item in AllocationMethod),
        default=AllocationMethod.EQUAL_WEIGHT.value,
    )
    parser.add_argument("--initial-equity", default="1000000")
    parser.add_argument("--annualization-periods", type=int, default=252)
    parser.add_argument("--rebalance-frequency", type=int, default=5)
    parser.add_argument("--volatility-lookback", type=int, default=20)
    parser.add_argument("--execution-delay-periods", type=int, default=1)
    parser.add_argument("--minimum-active-candidates", type=int, default=2)
    parser.add_argument("--minimum-cash-weight", default="0.05")
    parser.add_argument("--maximum-candidate-weight", default="0.40")
    parser.add_argument("--maximum-one-way-turnover", default="1")
    parser.add_argument("--transaction-cost-bps", default="10")
    parser.add_argument("--maximum-evidence-age-days", type=int, default=120)
    parser.add_argument("--maximum-drawdown-before-halt", default="-0.30")
    parser.add_argument("--minimum-observations", type=int, default=60)
    parser.add_argument("--minimum-base-return", default="0")
    parser.add_argument("--minimum-stress-return", default="-0.05")
    parser.add_argument("--maximum-stress-drawdown", default="-0.40")
    parser.add_argument("--expected-shortfall-fraction", default="0.05")
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        report = DeterministicHistoricalShadowRunner(
            evidence_manifest=load_shadow_evidence_manifest(
                arguments.evidence_manifest
            ),
            return_matrix=parse_candidate_returns_csv(arguments.returns_csv),
            policy=_policy(arguments),
        ).run()
        print(format_historical_shadow_report(report))
        if arguments.json_output is not None:
            AtomicJsonHistoricalShadowReportWriter(arguments.json_output).write(
                report
            )
    except (HistoricalShadowError, PortfolioPromotionError) as error:
        parser.error(str(error))


def format_historical_shadow_report(report: HistoricalShadowReport) -> str:
    scenario_lookup = {item.scenario: item for item in report.scenarios}
    base = scenario_lookup[ShadowScenarioName.BASE]
    equal_weight = scenario_lookup[ShadowScenarioName.EQUAL_WEIGHT_BASELINE]
    double_cost = scenario_lookup[ShadowScenarioName.DOUBLE_COST]
    extra_delay = scenario_lookup[ShadowScenarioName.EXTRA_EXECUTION_DELAY]
    removed = scenario_lookup[ShadowScenarioName.LARGEST_CANDIDATE_REMOVED]
    return "\n".join(
        (
            "Execution mode: HISTORICAL_SHADOW",
            "Network access: DISABLED",
            "Broker provider: NONE",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Report ID:                     {report.report_id}",
            f"Dataset digest:                {report.dataset_digest}",
            f"Manifest digest:               {report.manifest_digest}",
            f"Allocation method:             {report.policy.allocation_method.value}",
            f"Observations:                  {base.metrics.observation_count}",
            f"Base cumulative return:        {base.metrics.cumulative_return:.6%}",
            "Equal-weight return:            "
            f"{equal_weight.metrics.cumulative_return:.6%}",
            "Double-cost return:            "
            f"{double_cost.metrics.cumulative_return:.6%}",
            "Extra-delay return:            "
            f"{extra_delay.metrics.cumulative_return:.6%}",
            "Largest-candidate-removed:      "
            f"{removed.metrics.cumulative_return:.6%}",
            f"Maximum drawdown:              {base.metrics.maximum_drawdown:.6%}",
            f"Total turnover:                {base.metrics.total_turnover}",
            f"Total simulated cost:          {base.metrics.total_cost}",
            f"Journal entries:               {len(report.journal)}",
            f"Final decision:                {report.decision.value}",
            f"Report digest:                 {report.report_digest}",
        )
    )


def _policy(arguments: argparse.Namespace) -> HistoricalShadowPolicy:
    return HistoricalShadowPolicy(
        initial_equity=decimal_argument(
            arguments.initial_equity,
            "initial equity",
        ),
        annualization_periods=arguments.annualization_periods,
        allocation_method=AllocationMethod(arguments.allocation),
        rebalance_frequency=arguments.rebalance_frequency,
        volatility_lookback=arguments.volatility_lookback,
        execution_delay_periods=arguments.execution_delay_periods,
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
        maximum_drawdown_before_halt=decimal_argument(
            arguments.maximum_drawdown_before_halt,
            "maximum drawdown before halt",
        ),
        minimum_observations=arguments.minimum_observations,
        minimum_base_cumulative_return=decimal_argument(
            arguments.minimum_base_return,
            "minimum base return",
        ),
        minimum_stress_cumulative_return=decimal_argument(
            arguments.minimum_stress_return,
            "minimum stress return",
        ),
        maximum_stress_drawdown=decimal_argument(
            arguments.maximum_stress_drawdown,
            "maximum stress drawdown",
        ),
        expected_shortfall_fraction=decimal_argument(
            arguments.expected_shortfall_fraction,
            "expected-shortfall fraction",
        ),
    )


if __name__ == "__main__":
    main()
