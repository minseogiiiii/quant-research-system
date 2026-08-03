from world_quant_system.research.portfolio_promotion_models import PromotionDecision
from world_quant_system.research.portfolio_promotion_simulation import (
    run_portfolio_promotion_simulation,
)


def test_portfolio_promotion_simulation() -> None:
    result = run_portfolio_promotion_simulation()

    assert result.candidate_count == 3
    assert result.observation_count == 180
    assert result.cluster_count >= 2
    assert result.decision is PromotionDecision.PROMOTED_FOR_SHADOW_RESEARCH
    assert result.deterministic_digest_match
