from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from world_quant_system.credential_isolation.models import (
    CredentialIsolationReport,
)


class AtomicJsonCredentialIsolationReportWriter:
    """Durably replace a credential report without partial JSON output."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def write(self, report: CredentialIsolationReport) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    report.to_document(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            directory_descriptor = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
            raise
