from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from world_quant_system.research.robustness_models import RobustnessReport


class AtomicJsonRobustnessReportWriter:
    """Atomically persist one deterministic robustness report."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, report: RobustnessReport) -> None:
        if not isinstance(report, RobustnessReport):
            raise TypeError("Robustness writer requires a RobustnessReport.")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    report.to_document(),
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
