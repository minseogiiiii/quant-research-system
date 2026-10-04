from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from world_quant_system.research.portfolio_promotion_models import (
    PortfolioPromotionConfigurationError,
    PortfolioPromotionReport,
)


class AtomicJsonPortfolioPromotionReportWriter:
    """Atomically persist one deterministic portfolio-promotion report."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path).expanduser().resolve()
        if self._path.exists() and not self._path.is_file():
            raise PortfolioPromotionConfigurationError(
                "Portfolio report output must be a file path."
            )

    @property
    def path(self) -> Path:
        return self._path

    def write(self, report: PortfolioPromotionReport) -> Path:
        if not isinstance(report, PortfolioPromotionReport):
            raise PortfolioPromotionConfigurationError(
                "Portfolio report writer requires a PortfolioPromotionReport."
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = report.to_document()
        payload = (
            json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, 0o600)
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
        return self._path


__all__ = ["AtomicJsonPortfolioPromotionReportWriter"]
