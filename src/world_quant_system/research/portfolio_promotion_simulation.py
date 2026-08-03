from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.research.portfolio_promotion import (
    DeterministicPortfolioPromotionEngine,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    CandidateEvidence,
    CandidateReturnMatrix,
    PortfolioPromotionReport,
    PromotionDecision,
    PromotionPolicy,
)


@dataclass(frozen=True, slots=True)
class PortfolioPromotionSimulationResult:
    candidate_count: int
    observation_count: int
    cluster_count: int
    decision: PromotionDecision
    base_cumulative_return: Decimal
    adverse_cumulative_return: Decimal
    deterministic_digest_match: bool


def run_portfolio_promotion_simulation() -> PortfolioPromotionSimulationResult:
    first = build_portfolio_promotion_simulation_report(
        created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    second = build_portfolio_promotion_simulation_report(
        created_at=datetime(2026, 2, 1, tzinfo=UTC)
    )
    base = first.scenarios[0]
    adverse = first.scenarios[1]
    return PortfolioPromotionSimulationResult(
        candidate_count=len(first.candidate_evidence),
        observation_count=base.metrics.observation_count,
        cluster_count=len(first.redundancy_clusters),
        decision=first.decision,
        base_cumulative_return=base.metrics.cumulative_return,
        adverse_cumulative_return=adverse.metrics.cumulative_return,
        deterministic_digest_match=(
            first.report_digest == second.report_digest
            and first.report_id == second.report_id
        ),
    )


def build_portfolio_promotion_simulation_report(
    *,
    created_at: datetime | None = None,
) -> PortfolioPromotionReport:
    matrix, evidence, policy = _simulation_inputs()
    return DeterministicPortfolioPromotionEngine(
        candidate_evidence=evidence,
        return_matrix=matrix,
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        policy=policy,
    ).run(created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC))


def _simulation_inputs() -> tuple[
    CandidateReturnMatrix,
    tuple[CandidateEvidence, ...],
    PromotionPolicy,
]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    count = 180
    candidate_ids = ("alpha-a", "alpha-b", "alpha-c")
    returns = tuple(
        tuple(
            Decimal(
                str(
                    0.0012
                    + 0.0014 * math.sin(index * 0.21 + phase)
                    + 0.0003 * math.cos(index * 0.07 + phase * 2)
                )
            )
            for index in range(count)
        )
        for phase in (0.0, 1.7, 3.4)
    )
    matrix = CandidateReturnMatrix(
        timestamps=tuple(start + timedelta(days=index) for index in range(count)),
        candidate_ids=candidate_ids,
        returns_by_candidate=returns,
    )
    evidence = tuple(
        CandidateEvidence(
            candidate_id=candidate_id,
            strategy_name="synthetic-alpha",
            strategy_version="1.0.0",
            dataset_digest="a" * 64,
            backtest_file_sha256=hex_character * 64,
            robustness_file_sha256=robustness_character * 64,
            robustness_report_digest=robustness_digest_character * 64,
            robustness_passed=True,
            statistical_file_sha256=statistical_character * 64,
            statistical_report_digest=statistical_digest_character * 64,
            statistical_passed=True,
            declared_turnover=Decimal("8"),
            declared_cost_to_gross_profit_ratio=Decimal("0.15"),
        )
        for candidate_id, hex_character, robustness_character,
        robustness_digest_character, statistical_character,
        statistical_digest_character in (
            ("alpha-a", "1", "4", "7", "a", "d"),
            ("alpha-b", "2", "5", "8", "b", "e"),
            ("alpha-c", "3", "6", "9", "c", "f"),
        )
    )
    policy = PromotionPolicy(
        minimum_candidate_observations=120,
        maximum_candidate_weight=Decimal("0.45"),
        maximum_cluster_weight=Decimal("0.70"),
        adverse_cost_bps=Decimal("5"),
        severe_cost_bps=Decimal("15"),
        minimum_effective_strategy_count=2.0,
        maximum_severe_drawdown=Decimal("-0.25"),
    )
    return matrix, evidence, policy


def main() -> None:
    result = run_portfolio_promotion_simulation()
    print(f"Portfolio candidates: {result.candidate_count}")
    print(f"Portfolio observations: {result.observation_count}")
    print(f"Redundancy clusters: {result.cluster_count}")
    print(f"Base cumulative return: {result.base_cumulative_return:.6%}")
    print(f"Adverse cumulative return: {result.adverse_cumulative_return:.6%}")
    print(f"Promotion decision: {result.decision.value}")
    print(
        "Deterministic portfolio digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print("Network access: disabled")
    print("Broker provider: NONE")
    print("Live trading and broker orders: disabled")


if __name__ == "__main__":
    main()
