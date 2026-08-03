from __future__ import annotations

from decimal import Decimal

import pytest

from world_quant_system.research.portfolio_promotion_models import (
    CandidateEvidence,
    PortfolioPromotionConfigurationError,
    PromotionPolicy,
)


def test_candidate_evidence_requires_sha256_provenance() -> None:
    with pytest.raises(PortfolioPromotionConfigurationError, match="SHA-256"):
        CandidateEvidence(
            candidate_id="alpha",
            strategy_name="strategy",
            strategy_version="1",
            dataset_digest="bad",
            backtest_file_sha256="a" * 64,
            robustness_file_sha256="b" * 64,
            robustness_report_digest="c" * 64,
            robustness_passed=True,
            statistical_file_sha256="d" * 64,
            statistical_report_digest="e" * 64,
            statistical_passed=True,
            declared_turnover=Decimal("1"),
            declared_cost_to_gross_profit_ratio=Decimal("0.1"),
        )


def test_policy_rejects_candidate_cap_above_cluster_cap() -> None:
    with pytest.raises(PortfolioPromotionConfigurationError, match="cluster"):
        PromotionPolicy(
            maximum_candidate_weight=Decimal("0.7"),
            maximum_cluster_weight=Decimal("0.6"),
        )
