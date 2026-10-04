from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path

from world_quant_system.research.portfolio_promotion import (
    DeterministicPortfolioPromotionEngine,
    load_candidate_evidence_manifest,
    parse_candidate_returns_csv,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    PortfolioPromotionError,
    PortfolioPromotionReport,
    PromotionPolicy,
)
from world_quant_system.research.portfolio_promotion_reporting import (
    AtomicJsonPortfolioPromotionReportWriter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-portfolio",
        description=(
            "Run deterministic, networkless research-to-portfolio promotion "
            "and stress validation."
        ),
    )
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--returns-csv", type=Path, required=True)
    parser.add_argument(
        "--allocation",
        choices=tuple(item.value for item in AllocationMethod),
        default=AllocationMethod.EQUAL_WEIGHT.value,
    )
    parser.add_argument("--annualization-periods", type=int, default=252)
    parser.add_argument("--minimum-candidate-observations", type=int, default=60)
    parser.add_argument("--minimum-eligible-candidates", type=int, default=2)
    parser.add_argument("--minimum-candidate-return", default="0")
    parser.add_argument("--maximum-candidate-drawdown", default="-0.40")
    parser.add_argument("--maximum-declared-turnover", default="100")
    parser.add_argument("--maximum-cost-ratio", default="0.70")
    parser.add_argument("--volatility-lookback", type=int, default=20)
    parser.add_argument("--rebalance-frequency", type=int, default=5)
    parser.add_argument("--minimum-cash-weight", default="0.05")
    parser.add_argument("--maximum-candidate-weight", default="0.40")
    parser.add_argument("--maximum-cluster-weight", default="0.60")
    parser.add_argument("--maximum-pairwise-correlation", type=float, default=0.90)
    parser.add_argument(
        "--maximum-loss-period-correlation",
        type=float,
        default=0.90,
    )
    parser.add_argument("--minimum-loss-period-observations", type=int, default=8)
    parser.add_argument("--base-cost-bps", default="0")
    parser.add_argument("--adverse-cost-bps", default="10")
    parser.add_argument("--severe-cost-bps", default="25")
    parser.add_argument("--common-loss-multiplier", default="1.50")
    parser.add_argument("--minimum-base-return", default="0")
    parser.add_argument("--minimum-adverse-return", default="0")
    parser.add_argument("--maximum-severe-drawdown", default="-0.35")
    parser.add_argument("--minimum-effective-strategies", type=float, default=1.50)
    parser.add_argument("--walk-forward-folds", type=int, default=4)
    parser.add_argument("--minimum-fold-observations", type=int, default=15)
    parser.add_argument("--minimum-fold-return", default="-0.05")
    parser.add_argument("--maximum-fold-drawdown", default="-0.25")
    parser.add_argument("--minimum-fold-pass-rate", default="0.75")
    parser.add_argument("--expected-shortfall-fraction", default="0.05")
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        report = DeterministicPortfolioPromotionEngine(
            candidate_evidence=load_candidate_evidence_manifest(
                arguments.candidate_manifest
            ),
            return_matrix=parse_candidate_returns_csv(arguments.returns_csv),
            allocation_method=AllocationMethod(arguments.allocation),
            policy=_policy(arguments),
        ).run()
        print(format_portfolio_promotion_report(report))
        if arguments.json_output is not None:
            AtomicJsonPortfolioPromotionReportWriter(
                arguments.json_output
            ).write(report)
    except PortfolioPromotionError as error:
        parser.error(str(error))


def format_portfolio_promotion_report(
    report: PortfolioPromotionReport,
) -> str:
    eligible = sum(
        item.decision.value == "eligible"
        for item in report.candidate_results
    )
    rejected = sum(
        item.decision.value == "rejected"
        for item in report.candidate_results
    )
    insufficient = sum(
        item.decision.value == "insufficient_evidence"
        for item in report.candidate_results
    )
    base = next(
        item for item in report.scenarios if item.scenario.value == "base"
    )
    adverse = next(
        item for item in report.scenarios if item.scenario.value == "adverse_cost"
    )
    severe = next(
        item for item in report.scenarios if item.scenario.value == "severe_cost"
    )
    return "\n".join(
        (
            "Execution mode: PORTFOLIO_RESEARCH",
            "Network access: DISABLED",
            "Broker provider: NONE",
            "Live trading: DISABLED",
            "Order submission: DISABLED",
            "",
            f"Report ID:                  {report.report_id}",
            f"Dataset digest:             {report.dataset_digest}",
            f"Allocation method:          {report.allocation_method.value}",
            f"Candidates eligible:        {eligible}",
            f"Candidates rejected:        {rejected}",
            f"Insufficient evidence:      {insufficient}",
            f"Redundancy clusters:        {len(report.redundancy_clusters)}",
            f"Base cumulative return:     {base.metrics.cumulative_return:.6%}",
            f"Adverse cumulative return:  {adverse.metrics.cumulative_return:.6%}",
            f"Severe maximum drawdown:    {severe.metrics.maximum_drawdown:.6%}",
            "Effective strategy count:  "
            f"{base.metrics.average_effective_strategy_count:.4f}",
            "Walk-forward pass rate:      "
            f"{report.walk_forward.pass_rate:.2%}",
            "Worst fold return:           "
            f"{report.walk_forward.worst_cumulative_return:.6%}",
            f"Final decision:             {report.decision.value}",
            f"Report digest:              {report.report_digest}",
        )
    )


def _policy(arguments: argparse.Namespace) -> PromotionPolicy:
    return PromotionPolicy(
        annualization_periods=arguments.annualization_periods,
        minimum_candidate_observations=arguments.minimum_candidate_observations,
        minimum_eligible_candidates=arguments.minimum_eligible_candidates,
        minimum_candidate_cumulative_return=_decimal(
            arguments.minimum_candidate_return,
            "minimum candidate return",
        ),
        maximum_candidate_drawdown=_decimal(
            arguments.maximum_candidate_drawdown,
            "maximum candidate drawdown",
        ),
        maximum_declared_turnover=_decimal(
            arguments.maximum_declared_turnover,
            "maximum declared turnover",
        ),
        maximum_cost_to_gross_profit_ratio=_decimal(
            arguments.maximum_cost_ratio,
            "maximum cost ratio",
        ),
        volatility_lookback=arguments.volatility_lookback,
        rebalance_frequency=arguments.rebalance_frequency,
        minimum_cash_weight=_decimal(
            arguments.minimum_cash_weight,
            "minimum cash weight",
        ),
        maximum_candidate_weight=_decimal(
            arguments.maximum_candidate_weight,
            "maximum candidate weight",
        ),
        maximum_cluster_weight=_decimal(
            arguments.maximum_cluster_weight,
            "maximum cluster weight",
        ),
        maximum_pairwise_correlation=arguments.maximum_pairwise_correlation,
        maximum_loss_period_correlation=(
            arguments.maximum_loss_period_correlation
        ),
        minimum_loss_period_observations=(
            arguments.minimum_loss_period_observations
        ),
        base_cost_bps=_decimal(arguments.base_cost_bps, "base cost bps"),
        adverse_cost_bps=_decimal(
            arguments.adverse_cost_bps,
            "adverse cost bps",
        ),
        severe_cost_bps=_decimal(
            arguments.severe_cost_bps,
            "severe cost bps",
        ),
        common_loss_multiplier=_decimal(
            arguments.common_loss_multiplier,
            "common loss multiplier",
        ),
        minimum_base_cumulative_return=_decimal(
            arguments.minimum_base_return,
            "minimum base return",
        ),
        minimum_adverse_cumulative_return=_decimal(
            arguments.minimum_adverse_return,
            "minimum adverse return",
        ),
        maximum_severe_drawdown=_decimal(
            arguments.maximum_severe_drawdown,
            "maximum severe drawdown",
        ),
        minimum_effective_strategy_count=arguments.minimum_effective_strategies,
        walk_forward_folds=arguments.walk_forward_folds,
        minimum_fold_observations=arguments.minimum_fold_observations,
        minimum_fold_cumulative_return=_decimal(
            arguments.minimum_fold_return,
            "minimum fold return",
        ),
        maximum_fold_drawdown=_decimal(
            arguments.maximum_fold_drawdown,
            "maximum fold drawdown",
        ),
        minimum_fold_pass_rate=_decimal(
            arguments.minimum_fold_pass_rate,
            "minimum fold pass rate",
        ),
        expected_shortfall_fraction=_decimal(
            arguments.expected_shortfall_fraction,
            "expected-shortfall fraction",
        ),
    )


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise PortfolioPromotionError(
            f"{field_name} must be a valid decimal."
        ) from error
    if not parsed.is_finite():
        raise PortfolioPromotionError(f"{field_name} must be finite.")
    return parsed


if __name__ == "__main__":
    main()
