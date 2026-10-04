from __future__ import annotations

import os
import tempfile
from pathlib import Path

from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    BacktestRunResult,
    OrderRejectReason,
    OrderStatus,
    backtest_config_document,
    metrics_document,
)
from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.research import CorporateActionType


class AtomicJsonBacktestSummaryWriter:
    """Durably write a compact deterministic backtest summary."""

    def __init__(self, path: str | Path) -> None:
        if not isinstance(path, (str, Path)):
            raise BacktestConfigurationError(
                "Backtest JSON output path must be a string or Path."
            )
        destination = Path(path).expanduser().resolve()
        if not destination.name:
            raise BacktestConfigurationError(
                "Backtest JSON output path must include a filename."
            )
        self._destination = destination

    @property
    def destination(self) -> Path:
        return self._destination

    def write(self, result: BacktestRunResult) -> None:
        if not isinstance(result, BacktestRunResult):
            raise BacktestConfigurationError(
                "Backtest result writer requires a BacktestRunResult."
            )
        destination = self._destination
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = canonical_json_bytes(backtest_summary_document(result))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise


def backtest_summary_document(result: BacktestRunResult) -> dict[str, object]:
    return {
        "execution_mode": "replay",
        "network_access": "disabled",
        "live_trading": "disabled",
        "order_submission": "disabled",
        "configuration": backtest_config_document(result.config),
        "strategy": {
            "name": result.strategy.name,
            "version": result.strategy.version,
            "parameters": dict(result.strategy.parameters),
            "fingerprint": result.strategy.fingerprint,
        },
        "config_fingerprint": result.config_fingerprint,
        "replay_digest": result.replay_result.event_digest,
        "run_digest": result.run_digest,
        "event_count": result.replay_result.event_count,
        "signal_count": len(result.signals),
        "pass_count": result.replay_result.pass_count,
        "warning_count": result.replay_result.warning_count,
        "order_count": len(result.orders),
        "order_status_counts": {
            status.value: sum(
                record.status is status for record in result.orders
            )
            for status in OrderStatus
        },
        "order_reject_reason_counts": {
            reason.value: sum(
                record.reject_reason is reason for record in result.orders
            )
            for reason in OrderRejectReason
        },
        "fill_count": len(result.fills),
        "closed_trade_count": len(result.trades),
        "corporate_action_count": len(result.corporate_actions),
        "corporate_action_type_counts": {
            action_type.value: sum(
                application.action_type is action_type
                for application in result.corporate_actions
            )
            for action_type in CorporateActionType
        },
        "corporate_actions": [
            application.to_document()
            for application in result.corporate_actions
        ],
        "metrics": metrics_document(result.metrics),
    }
