from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from world_quant_system.research.models import ResearchWindow
from world_quant_system.research.robustness_models import (
    MarketRegime,
    PhaseResult,
    RobustnessCaseResult,
    RobustnessPhase,
    RobustnessPolicy,
    RobustnessReport,
    RobustnessStrategySpec,
    StressScenario,
    WalkForwardPlan,
)
from world_quant_system.research.robustness_reporting import (
    AtomicJsonRobustnessReportWriter,
)


def test_atomic_writer_persists_complete_report(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    plan = WalkForwardPlan.rolling(
        start=start,
        train_duration=timedelta(days=20),
        validation_duration=timedelta(days=10),
        test_duration=timedelta(days=10),
        step=timedelta(days=10),
        fold_count=1,
    )
    fold = plan.folds[0]
    strategy = RobustnessStrategySpec(
        name="sma-crossover",
        short_window=5,
        long_window=10,
    )
    scenario = StressScenario(Decimal("1"), 1)
    train = _phase(RobustnessPhase.TRAIN, fold.train, "a" * 64)
    validation = _phase(RobustnessPhase.VALIDATION, fold.validation, "b" * 64)
    test = _phase(RobustnessPhase.TEST, fold.test, "c" * 64)
    case = RobustnessCaseResult.build(
        fold_number=1,
        strategy=strategy,
        scenario=scenario,
        train=train,
        validation=validation,
        test=test,
        regime=MarketRegime.SIDEWAYS,
        return_degradation=Decimal("0"),
        sharpe_degradation=0.0,
        passed=True,
        failure_reasons=(),
    )
    report = RobustnessReport.build(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        dataset_digest="d" * 64,
        base_config_fingerprint="e" * 64,
        plan=plan,
        policy=RobustnessPolicy(required_pass_rate=Decimal("1")),
        cases=(case,),
    )
    output = tmp_path / "report.json"

    AtomicJsonRobustnessReportWriter(output).write(report)

    document = json.loads(output.read_text())
    assert document["report_id"] == report.report_id
    assert document["report_digest"] == report.report_digest
    assert document["cases"][0]["case_id"] == case.case_id
    assert not tuple(tmp_path.glob("*.tmp"))


def _phase(
    phase: RobustnessPhase,
    window: ResearchWindow,
    digest: str,
) -> PhaseResult:
    return PhaseResult(
        phase=phase,
        window=window,
        event_count=20,
        run_digest=digest,
        total_return=Decimal("0.01"),
        maximum_drawdown=Decimal("-0.01"),
        sharpe_ratio=0.1,
        annualized_volatility=0.1,
        benchmark_return=Decimal("0"),
        commission_cost=Decimal("0"),
        slippage_cost=Decimal("0"),
    )
