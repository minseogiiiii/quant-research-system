from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from world_quant_system.toss_live_read.models import (
    TossLiveReadCertificationReport,
    TossLiveReadError,
)


class AtomicJsonTossLiveReadReportWriter:
    def __init__(self, output_path: str | Path) -> None:
        self._output_path = Path(output_path).expanduser().resolve()

    def write(self, report: TossLiveReadCertificationReport) -> None:
        if not isinstance(report, TossLiveReadCertificationReport):
            raise TossLiveReadError(
                "Live-read writer requires a certification report."
            )
        parent = self._output_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            report.to_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._output_path.name}.",
            )
            temporary_path = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._output_path)
            directory_descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
