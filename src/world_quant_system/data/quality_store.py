from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from world_quant_system.data.quality_models import (
    DataQualityConfigurationError,
    DataQualityReport,
    DataQualityReportNotFoundError,
    DataQualityStoreConflictError,
    DataQualityStoreError,
    DataQualityStoreIntegrityError,
    QualityStatus,
    canonical_json_bytes,
    report_from_document,
    report_semantic_fingerprint,
    report_to_document,
)

_SCHEMA_VERSION = 1
_COLUMNS = """
    report_id,
    assessment_key,
    record_id,
    raw_content_sha256,
    dataset_kind,
    status,
    checked_at,
    validator_version,
    policy_fingerprint,
    item_count,
    report_json,
    report_sha256,
    semantic_fingerprint
"""


class SQLiteDataQualityStore:
    """Durable, idempotent quality reports and quarantine index."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._root, 0o700)
        except OSError as error:
            raise DataQualityStoreIntegrityError(
                "Could not secure the quality-store directory."
            ) from error
        self._catalog_path = self._root / "quality.sqlite3"
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    async def save(self, report: DataQualityReport) -> DataQualityReport:
        if not isinstance(report, DataQualityReport):
            raise DataQualityConfigurationError(
                "Quality store can persist only DataQualityReport values."
            )
        return await asyncio.to_thread(self._save_sync, report)

    async def get(self, report_id: str) -> DataQualityReport:
        return await asyncio.to_thread(self._get_sync, report_id)

    async def query(
        self,
        *,
        status: QualityStatus | None = None,
        record_id: str | None = None,
        limit: int = 1_000,
    ) -> tuple[DataQualityReport, ...]:
        return await asyncio.to_thread(
            self._query_sync,
            status,
            record_id,
            limit,
        )

    async def quarantined(
        self,
        *,
        limit: int = 1_000,
    ) -> tuple[DataQualityReport, ...]:
        return await self.query(
            status=QualityStatus.QUARANTINE,
            limit=limit,
        )

    def _save_sync(self, report: DataQualityReport) -> DataQualityReport:
        document = report_to_document(report)
        serialized = canonical_json_bytes(document)
        report_sha256 = hashlib.sha256(serialized).hexdigest()
        semantic_fingerprint = report_semantic_fingerprint(report)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM quality_reports "
                "WHERE assessment_key = ?",
                (report.assessment_key,),
            ).fetchone()
            if row is not None:
                existing = self._report_from_row(row)
                existing_fingerprint = cast(str, row[12])
                if existing_fingerprint != semantic_fingerprint:
                    raise DataQualityStoreConflictError(
                        "Assessment key already exists with different results."
                    )
                return existing

            connection.execute(
                """
                INSERT INTO quality_reports(
                    report_id,
                    assessment_key,
                    record_id,
                    raw_content_sha256,
                    dataset_kind,
                    status,
                    checked_at,
                    validator_version,
                    policy_fingerprint,
                    item_count,
                    report_json,
                    report_sha256,
                    semantic_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.assessment_key,
                    report.record_id,
                    report.raw_content_sha256,
                    report.dataset_kind.value,
                    report.status.value,
                    report.checked_at.isoformat(),
                    report.validator_version,
                    report.policy_fingerprint,
                    report.item_count,
                    serialized.decode("utf-8"),
                    report_sha256,
                    semantic_fingerprint,
                ),
            )

        return report

    def _get_sync(self, report_id: str) -> DataQualityReport:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM quality_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            raise DataQualityReportNotFoundError(
                "Quality report does not exist."
            )
        return self._report_from_row(row)

    def _query_sync(
        self,
        status: QualityStatus | None,
        record_id: str | None,
        limit: int,
    ) -> tuple[DataQualityReport, ...]:
        if status is not None and not isinstance(status, QualityStatus):
            raise DataQualityConfigurationError(
                "Quality status filter must be a QualityStatus value."
            )
        if record_id is not None and (
            not isinstance(record_id, str) or not record_id.strip()
        ):
            raise DataQualityConfigurationError(
                "Record ID filter must be a nonblank string or None."
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise DataQualityConfigurationError(
                "Quality query limit must be between 1 and 10000."
            )

        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        if record_id is not None:
            clauses.append("record_id = ?")
            values.append(record_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)

        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM quality_reports {where} "
                "ORDER BY checked_at ASC, report_id ASC LIMIT ?",
                values,
            ).fetchall()
        return tuple(self._report_from_row(row) for row in rows)

    def _report_from_row(self, row: Sequence[object]) -> DataQualityReport:
        report_json = row[10]
        expected_sha256 = row[11]
        expected_fingerprint = row[12]
        if not all(
            isinstance(value, str)
            for value in (report_json, expected_sha256, expected_fingerprint)
        ):
            raise DataQualityStoreIntegrityError(
                "Stored quality report catalog row is invalid."
            )
        serialized = cast(str, report_json).encode("utf-8")
        if hashlib.sha256(serialized).hexdigest() != expected_sha256:
            raise DataQualityStoreIntegrityError(
                "Stored quality report failed SHA-256 verification."
            )
        try:
            document = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise DataQualityStoreIntegrityError(
                "Stored quality report is not valid JSON."
            ) from error
        report = report_from_document(document)
        if report_semantic_fingerprint(report) != expected_fingerprint:
            raise DataQualityStoreIntegrityError(
                "Stored quality report semantic fingerprint is invalid."
            )
        catalog_values = (
            report.report_id,
            report.assessment_key,
            report.record_id,
            report.raw_content_sha256,
            report.dataset_kind.value,
            report.status.value,
            report.checked_at.isoformat(),
            report.validator_version,
            report.policy_fingerprint,
            report.item_count,
        )
        row_values = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
        )
        if catalog_values != row_values:
            raise DataQualityStoreIntegrityError(
                "Stored quality report does not match catalog metadata."
            )
        return report

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS quality_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quality_reports (
                    report_id TEXT PRIMARY KEY,
                    assessment_key TEXT NOT NULL UNIQUE,
                    record_id TEXT NOT NULL,
                    raw_content_sha256 TEXT NOT NULL,
                    dataset_kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PASS', 'WARNING', 'QUARANTINE')
                    ),
                    checked_at TEXT NOT NULL,
                    validator_version TEXT NOT NULL,
                    policy_fingerprint TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    semantic_fingerprint TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_quality_status_time
                ON quality_reports(status, checked_at, report_id);

                CREATE INDEX IF NOT EXISTS idx_quality_record
                ON quality_reports(record_id, checked_at, report_id);
                """
            )
            row = connection.execute(
                "SELECT value FROM quality_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO quality_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
            elif row[0] != str(_SCHEMA_VERSION):
                raise DataQualityConfigurationError(
                    "Quality-store schema version is unsupported."
                )
        try:
            os.chmod(self._catalog_path, 0o600)
        except OSError as error:
            raise DataQualityStoreIntegrityError(
                "Could not secure the quality-store catalog."
            ) from error

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self._catalog_path,
                timeout=30.0,
            )
        except sqlite3.Error as error:
            raise DataQualityStoreError(
                "Could not open the quality-store catalog."
            ) from error
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()
