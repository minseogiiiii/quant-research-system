from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowConfigurationError,
    HistoricalShadowReport,
)


class AtomicJsonHistoricalShadowReportWriter:
    """Persist a historical-shadow report atomically and durably."""

    def __init__(self, output_path: Path) -> None:
        if not isinstance(output_path, Path):
            raise HistoricalShadowConfigurationError(
                "Historical shadow output path must be a pathlib.Path."
            )
        self._output_path = output_path

    def write(self, report: HistoricalShadowReport) -> None:
        if not isinstance(report, HistoricalShadowReport):
            raise HistoricalShadowConfigurationError(
                "Historical shadow writer requires a HistoricalShadowReport."
            )
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._output_path.name}.",
            suffix=".tmp",
            dir=self._output_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    report.to_document(),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._output_path)
            directory_descriptor = os.open(
                self._output_path.parent,
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
            raise
