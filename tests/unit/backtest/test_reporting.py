import json
from pathlib import Path

from world_quant_system.backtest import AtomicJsonBacktestSummaryWriter
from world_quant_system.backtest.simulation import run_strategy_backtest_simulation


def test_atomic_json_writer_is_deterministic_and_records_safety_state(
    tmp_path: Path,
) -> None:
    result = run_strategy_backtest_simulation().buy_and_hold
    destination = tmp_path / "nested" / "summary.json"
    writer = AtomicJsonBacktestSummaryWriter(destination)
    writer.write(result)
    first = destination.read_bytes()
    writer.write(result)
    assert destination.read_bytes() == first

    document = json.loads(first)
    assert document["execution_mode"] == "replay"
    assert document["network_access"] == "disabled"
    assert document["live_trading"] == "disabled"
    assert document["order_submission"] == "disabled"
    assert document["configuration"]["symbol"] == "005930"
    assert document["signal_count"] == result.replay_result.event_count
    assert document["order_status_counts"]["filled"] >= 1
    assert document["run_digest"] == result.run_digest
