import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from world_quant_system.data import (
    DataQualityReport,
    DataQualityStoreConflictError,
    DataQualityStoreIntegrityError,
    QualityDatasetKind,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    QualityStatus,
    SQLiteDataQualityStore,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
RAW_SHA = "a" * 64
ASSESSMENT_KEY = "b" * 64


def report(
    *,
    report_id: str | None = None,
    status: QualityStatus = QualityStatus.PASS,
    assessment_key: str = ASSESSMENT_KEY,
    message: str = "warning",
) -> DataQualityReport:
    issues: tuple[QualityIssue, ...] = ()
    if status is QualityStatus.WARNING:
        issues = (
            QualityIssue(
                code=QualityIssueCode.STALE_QUOTE,
                severity=QualitySeverity.WARNING,
                message=message,
            ),
        )
    elif status is QualityStatus.QUARANTINE:
        issues = (
            QualityIssue(
                code=QualityIssueCode.RAW_INTEGRITY_FAILURE,
                severity=QualitySeverity.QUARANTINE,
                message=message,
            ),
        )
    return DataQualityReport(
        report_id=report_id or str(uuid4()),
        assessment_key=assessment_key,
        record_id=str(uuid4()),
        raw_content_sha256=RAW_SHA,
        dataset_kind=QualityDatasetKind.QUOTES,
        status=status,
        checked_at=NOW,
        validator_version="1.0.0",
        policy_fingerprint="d" * 64,
        item_count=1,
        issues=issues,
    )


@pytest.mark.asyncio
async def test_save_get_query_and_quarantine_round_trip(tmp_path: Path) -> None:
    store = SQLiteDataQualityStore(tmp_path / "quality")
    passed = report()
    quarantined = report(
        status=QualityStatus.QUARANTINE,
        assessment_key="c" * 64,
    )

    assert await store.save(passed) == passed
    assert await store.save(quarantined) == quarantined
    assert await store.get(passed.report_id) == passed
    assert await store.query(status=QualityStatus.PASS) == (passed,)
    assert await store.quarantined() == (quarantined,)


@pytest.mark.asyncio
async def test_same_assessment_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    store = SQLiteDataQualityStore(tmp_path / "quality")
    candidate = report()

    results = await asyncio.gather(*(store.save(candidate) for _ in range(100)))

    assert all(item == candidate for item in results)
    assert await store.query() == (candidate,)


@pytest.mark.asyncio
async def test_assessment_key_conflict_is_rejected(tmp_path: Path) -> None:
    store = SQLiteDataQualityStore(tmp_path / "quality")
    await store.save(report(status=QualityStatus.WARNING))

    with pytest.raises(DataQualityStoreConflictError):
        await store.save(
            report(
                status=QualityStatus.WARNING,
                message="different warning",
            )
        )


@pytest.mark.asyncio
async def test_catalog_tampering_is_detected(tmp_path: Path) -> None:
    store = SQLiteDataQualityStore(tmp_path / "quality")
    saved = await store.save(report())
    catalog = store.root / "quality.sqlite3"
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            "UPDATE quality_reports SET report_json = ? WHERE report_id = ?",
            ("{}", saved.report_id),
        )

    with pytest.raises(DataQualityStoreIntegrityError):
        await store.get(saved.report_id)


def test_sqlite_connections_are_closed_after_each_quality_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingConnection(sqlite3.Connection):
        is_closed = False

        def close(self) -> None:
            self.is_closed = True
            super().close()

    original_connect = sqlite3.connect
    opened_connections: list[TrackingConnection] = []

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = cast(
            TrackingConnection,
            original_connect(*args, **kwargs),
        )
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    store = SQLiteDataQualityStore(tmp_path / "quality")

    async def exercise() -> None:
        saved = await store.save(report())
        await store.get(saved.report_id)
        await store.query()
        await store.quarantined()

    asyncio.run(exercise())

    assert opened_connections
    assert all(connection.is_closed for connection in opened_connections)
