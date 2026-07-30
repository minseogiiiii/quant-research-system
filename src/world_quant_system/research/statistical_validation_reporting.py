from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from world_quant_system.research.statistical_validation_models import (
    StatisticalValidationError,
    StatisticalValidationReport,
)


class AtomicJsonStatisticalValidationReportWriter:
    def __init__(self, output_path: Path | str) -> None:
        self._output_path = Path(output_path).expanduser().resolve()

    @property
    def output_path(self) -> Path:
        return self._output_path

    def write(self, report: StatisticalValidationReport) -> None:
        if not isinstance(report, StatisticalValidationReport):
            raise StatisticalValidationError(
                "Statistical report writer requires a StatisticalValidationReport."
            )
        parent = self._output_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            report.to_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=parent,
                prefix=f".{self._output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._output_path)
            os.chmod(self._output_path, 0o600)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StatisticalValidationError(
                "Could not atomically persist the statistical validation report."
            ) from error
