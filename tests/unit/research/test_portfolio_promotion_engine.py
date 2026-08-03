from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.research.portfolio_promotion import (
    DeterministicPortfolioPromotionEngine,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    CandidateDecision,
    CandidateEvidence,
    CandidateReturnMatrix,
    PortfolioPromotionReport,
    PromotionPolicy,
)


def test_higher_costs_do_not_improve_portfolio_return() -> None:
    report = _report()
    base, adverse, severe = report.scenarios[:3]

    assert base.metrics.cumulative_return >= adverse.metrics.cumulative_return
    assert adverse.metrics.cumulative_return >= severe.metrics.cumulative_return


def test_inverse_volatility_does_not_use_future_returns() -> None:
    matrix = _matrix()
    modified_series = list(matrix.returns_by_candidate)
    modified = list(modified_series[0])
    for index in range(80, len(modified)):
        modified[index] = Decimal("0.20") if index % 2 == 0 else Decimal("-0.15")
    modified_series[0] = tuple(modified)
    future_changed = CandidateReturnMatrix(
        timestamps=matrix.timestamps,
        candidate_ids=matrix.candidate_ids,
        returns_by_candidate=tuple(modified_series),
    )
    policy = _policy()
    first = DeterministicPortfolioPromotionEngine(
        candidate_evidence=_evidence(),
        return_matrix=matrix,
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = DeterministicPortfolioPromotionEngine(
        candidate_evidence=_evidence(),
        return_matrix=future_changed,
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))

    first_early = tuple(
        item.to_document()
        for item in first.allocation_snapshots
        if item.timestamp < matrix.timestamps[80]
    )
    second_early = tuple(
        item.to_document()
        for item in second.allocation_snapshots
        if item.timestamp < matrix.timestamps[80]
    )
    assert first_early == second_early


def test_failed_statistical_evidence_is_not_promoted() -> None:
    evidence = list(_evidence())
    failed = evidence[0]
    evidence[0] = CandidateEvidence(
        candidate_id=failed.candidate_id,
        strategy_name=failed.strategy_name,
        strategy_version=failed.strategy_version,
        dataset_digest=failed.dataset_digest,
        backtest_file_sha256=failed.backtest_file_sha256,
        robustness_file_sha256=failed.robustness_file_sha256,
        robustness_report_digest=failed.robustness_report_digest,
        robustness_passed=True,
        statistical_file_sha256=failed.statistical_file_sha256,
        statistical_report_digest=failed.statistical_report_digest,
        statistical_passed=False,
        declared_turnover=failed.declared_turnover,
        declared_cost_to_gross_profit_ratio=(
            failed.declared_cost_to_gross_profit_ratio
        ),
    )
    report = DeterministicPortfolioPromotionEngine(
        candidate_evidence=evidence,
        return_matrix=_matrix(),
        allocation_method=AllocationMethod.EQUAL_WEIGHT,
        policy=_policy(),
    ).run()

    result = next(
        item for item in report.candidate_results if item.candidate_id == "alpha-a"
    )
    assert result.decision is CandidateDecision.REJECTED
    assert sum(
        item.decision is CandidateDecision.ELIGIBLE
        for item in report.candidate_results
    ) == 2


def test_weight_and_cluster_caps_are_respected() -> None:
    report = _report(
        policy=PromotionPolicy(
            minimum_candidate_observations=60,
            maximum_pairwise_correlation=0.50,
            maximum_loss_period_correlation=0.50,
            maximum_candidate_weight=Decimal("0.40"),
            maximum_cluster_weight=Decimal("0.55"),
            adverse_cost_bps=Decimal("1"),
            severe_cost_bps=Decimal("2"),
            maximum_severe_drawdown=Decimal("-0.90"),
            minimum_effective_strategy_count=1.0,
        )
    )
    cluster_by_candidate = {
        candidate_id: cluster.cluster_id
        for cluster in report.redundancy_clusters
        for candidate_id in cluster.candidate_ids
    }
    for snapshot in report.allocation_snapshots:
        assert all(item.weight <= Decimal("0.40") for item in snapshot.weights)
        cluster_totals: dict[str, Decimal] = {}
        for item in snapshot.weights:
            cluster_id = cluster_by_candidate[item.candidate_id]
            cluster_totals[cluster_id] = (
                cluster_totals.get(cluster_id, Decimal("0")) + item.weight
            )
        assert all(value <= Decimal("0.55") for value in cluster_totals.values())


def _report(
    *,
    policy: PromotionPolicy | None = None,
) -> PortfolioPromotionReport:
    return DeterministicPortfolioPromotionEngine(
        candidate_evidence=_evidence(),
        return_matrix=_matrix(),
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        policy=policy or _policy(),
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))


def _policy() -> PromotionPolicy:
    return PromotionPolicy(
        minimum_candidate_observations=60,
        adverse_cost_bps=Decimal("5"),
        severe_cost_bps=Decimal("15"),
        maximum_severe_drawdown=Decimal("-0.90"),
        minimum_effective_strategy_count=1.0,
    )


def _evidence() -> tuple[CandidateEvidence, ...]:
    values = (
        ("alpha-a", "1", "4", "7", "a", "d"),
        ("alpha-b", "2", "5", "8", "b", "e"),
        ("alpha-c", "3", "6", "9", "c", "f"),
    )
    return tuple(
        CandidateEvidence(
            candidate_id=candidate_id,
            strategy_name="strategy",
            strategy_version="1.0.0",
            dataset_digest="a" * 64,
            backtest_file_sha256=backtest_character * 64,
            robustness_file_sha256=robustness_character * 64,
            robustness_report_digest=robustness_digest_character * 64,
            robustness_passed=True,
            statistical_file_sha256=statistical_character * 64,
            statistical_report_digest=statistical_digest_character * 64,
            statistical_passed=True,
            declared_turnover=Decimal("5"),
            declared_cost_to_gross_profit_ratio=Decimal("0.1"),
        )
        for candidate_id, backtest_character, robustness_character,
        robustness_digest_character, statistical_character,
        statistical_digest_character in values
    )


def _matrix() -> CandidateReturnMatrix:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    count = 120
    first = tuple(
        Decimal("0.003") if index % 3 else Decimal("-0.001")
        for index in range(count)
    )
    second = tuple(
        Decimal("0.0025") if index % 4 else Decimal("-0.0012")
        for index in range(count)
    )
    third = tuple(
        Decimal("0.002") if index % 5 else Decimal("-0.0015")
        for index in range(count)
    )
    return CandidateReturnMatrix(
        timestamps=tuple(start + timedelta(days=index) for index in range(count)),
        candidate_ids=("alpha-a", "alpha-b", "alpha-c"),
        returns_by_candidate=(first, second, third),
    )


def test_walk_forward_failure_rejects_portfolio() -> None:
    matrix = _matrix()
    stressed = []
    for series in matrix.returns_by_candidate:
        values = list(series)
        for index in range(30):
            values[index] = Decimal("-0.003")
        stressed.append(tuple(values))
    report = DeterministicPortfolioPromotionEngine(
        candidate_evidence=_evidence(),
        return_matrix=CandidateReturnMatrix(
            timestamps=matrix.timestamps,
            candidate_ids=matrix.candidate_ids,
            returns_by_candidate=tuple(stressed),
        ),
        allocation_method=AllocationMethod.EQUAL_WEIGHT,
        policy=PromotionPolicy(
            minimum_candidate_observations=60,
            minimum_candidate_cumulative_return=Decimal("-0.50"),
            maximum_candidate_drawdown=Decimal("-0.90"),
            minimum_base_cumulative_return=Decimal("-0.50"),
            minimum_adverse_cumulative_return=Decimal("-0.50"),
            maximum_severe_drawdown=Decimal("-0.90"),
            minimum_effective_strategy_count=1.0,
            minimum_fold_cumulative_return=Decimal("0"),
            minimum_fold_pass_rate=Decimal("1"),
        ),
    ).run()

    assert report.walk_forward.passed is False
    assert report.decision.value == "rejected"
    assert "walk-forward" in " ".join(report.decision_reasons)


def test_weights_and_cash_never_exceed_total_capital() -> None:
    report = _report()

    for snapshot in report.allocation_snapshots:
        invested = sum(
            (item.weight for item in snapshot.weights),
            Decimal("0"),
        )
        assert invested >= Decimal("0")
        assert snapshot.cash_weight >= Decimal("0")
        assert invested + snapshot.cash_weight == Decimal("1")
