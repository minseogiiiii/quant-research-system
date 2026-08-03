from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from world_quant_system.paper_execution.models import (
    BrokerCertificationReport,
    PaperExecutionIntegrityError,
    PreTradeDecision,
    ReconciliationReport,
)


class AtomicJsonPaperReportWriter:
    """Durably replace a JSON report without exposing partial content."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        report: (
            BrokerCertificationReport
            | PreTradeDecision
            | ReconciliationReport
            | dict[str, object]
        ),
    ) -> None:
        document = report if isinstance(report, dict) else report.to_document()
        _atomic_json_write(self._path, document)


def load_json_document(path: str | Path) -> dict[str, object]:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as error:
        raise PaperExecutionIntegrityError(
            f"Unable to read JSON document: {target}."
        ) from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PaperExecutionIntegrityError(
            f"JSON document is invalid: {target}."
        ) from error
    if not isinstance(document, dict):
        raise PaperExecutionIntegrityError(
            f"JSON document must contain an object: {target}."
        )
    return document


def _atomic_json_write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    directory_descriptor: int | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        os.fsync(directory_descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
