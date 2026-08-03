from __future__ import annotations

import json
from pathlib import Path

from world_quant_system.research.portfolio_promotion_reporting import (
    AtomicJsonPortfolioPromotionReportWriter,
)
from world_quant_system.research.portfolio_promotion_simulation import (
    build_portfolio_promotion_simulation_report,
    run_portfolio_promotion_simulation,
)


def test_simulation_is_deterministic() -> None:
    result = run_portfolio_promotion_simulation()
    assert result.deterministic_digest_match


def test_atomic_writer_uses_restrictive_permissions(tmp_path: Path) -> None:
    report = build_portfolio_promotion_simulation_report()
    output = AtomicJsonPortfolioPromotionReportWriter(
        tmp_path / "portfolio.json"
    ).write(report)
    document = json.loads(output.read_text(encoding="utf-8"))

    assert document["report_digest"] == report.report_digest
    assert output.stat().st_mode & 0o777 == 0o600
