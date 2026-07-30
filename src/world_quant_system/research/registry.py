from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.research.models import (
    ExperimentOutcome,
    ExperimentRecord,
    ExperimentSnapshot,
    ExperimentSpec,
    HoldoutConsumedError,
    HoldoutConsumption,
    ResearchConfigurationError,
    ResearchConflictError,
    ResearchError,
    ResearchIntegrityError,
    ResearchNotFoundError,
    experiment_outcome_from_document,
    experiment_record_from_document,
    holdout_consumption_from_document,
)

_SCHEMA_VERSION = 1
_EXPERIMENT_COLUMNS = """
    experiment_id,
    research_digest,
    registered_at,
    parent_experiment_id,
    spec_json,
    spec_sha256
"""
_OUTCOME_COLUMNS = """
    experiment_id,
    status,
    completed_at,
    result_digest,
    failure_reason,
    outcome_json,
    outcome_sha256
"""
_HOLDOUT_COLUMNS = """
    experiment_id,
    consumed_at,
    result_digest,
    consumption_json,
    consumption_sha256
"""


class SQLiteExperimentRegistry:
    """Durable, immutable experiment metadata with fail-closed holdout use."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._root, 0o700)
        except OSError as error:
            raise ResearchIntegrityError(
                "Could not secure the research-registry directory."
            ) from error
        self._catalog_path = self._root / "research.sqlite3"
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    async def register(
        self,
        spec: ExperimentSpec,
        *,
        registered_at: datetime | None = None,
    ) -> ExperimentRecord:
        if not isinstance(spec, ExperimentSpec):
            raise ResearchConfigurationError(
                "Experiment registration requires an ExperimentSpec."
            )
        timestamp = registered_at or datetime.now(UTC)
        return await asyncio.to_thread(self._register_sync, spec, timestamp)

    async def get(self, experiment_id: str) -> ExperimentSnapshot:
        return await asyncio.to_thread(self._get_sync, experiment_id)

    async def query(self, *, limit: int = 1_000) -> tuple[ExperimentSnapshot, ...]:
        return await asyncio.to_thread(self._query_sync, limit)

    async def record_outcome(
        self,
        outcome: ExperimentOutcome,
    ) -> ExperimentOutcome:
        if not isinstance(outcome, ExperimentOutcome):
            raise ResearchConfigurationError(
                "Outcome recording requires an ExperimentOutcome."
            )
        return await asyncio.to_thread(self._record_outcome_sync, outcome)

    async def consume_holdout(
        self,
        consumption: HoldoutConsumption,
    ) -> HoldoutConsumption:
        if not isinstance(consumption, HoldoutConsumption):
            raise ResearchConfigurationError(
                "Holdout consumption requires a HoldoutConsumption."
            )
        return await asyncio.to_thread(self._consume_holdout_sync, consumption)

    def _register_sync(
        self,
        spec: ExperimentSpec,
        registered_at: datetime,
    ) -> ExperimentRecord:
        record = ExperimentRecord(
            experiment_id=spec.experiment_id,
            research_digest=spec.research_digest,
            spec=spec,
            registered_at=registered_at,
        )
        serialized = canonical_json_bytes(record.to_document())
        content_sha256 = hashlib.sha256(serialized).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if spec.parent_experiment_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM experiments WHERE experiment_id = ?",
                    (spec.parent_experiment_id,),
                ).fetchone()
                if parent is None:
                    raise ResearchNotFoundError(
                        "Parent experiment does not exist in this registry."
                    )
            row = connection.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments "
                "WHERE research_digest = ?",
                (spec.research_digest,),
            ).fetchone()
            if row is not None:
                existing = self._record_from_row(row)
                if existing.spec != spec:
                    raise ResearchConflictError(
                        "Research digest already exists with a different specification."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO experiments(
                    experiment_id,
                    research_digest,
                    registered_at,
                    parent_experiment_id,
                    spec_json,
                    spec_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.experiment_id,
                    record.research_digest,
                    record.registered_at.astimezone(UTC).isoformat(),
                    record.spec.parent_experiment_id,
                    serialized.decode("utf-8"),
                    content_sha256,
                ),
            )
        return record

    def _record_outcome_sync(
        self,
        outcome: ExperimentOutcome,
    ) -> ExperimentOutcome:
        serialized = canonical_json_bytes(outcome.to_document())
        content_sha256 = hashlib.sha256(serialized).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_experiment(connection, outcome.experiment_id)
            row = connection.execute(
                f"SELECT {_OUTCOME_COLUMNS} FROM experiment_outcomes "
                "WHERE experiment_id = ?",
                (outcome.experiment_id,),
            ).fetchone()
            if row is not None:
                existing = self._outcome_from_row(row)
                if (
                    existing.status is not outcome.status
                    or existing.result_digest != outcome.result_digest
                    or existing.failure_reason != outcome.failure_reason
                ):
                    raise ResearchConflictError(
                        "Experiment outcome is immutable and already differs."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO experiment_outcomes(
                    experiment_id,
                    status,
                    completed_at,
                    result_digest,
                    failure_reason,
                    outcome_json,
                    outcome_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.experiment_id,
                    outcome.status.value,
                    outcome.completed_at.astimezone(UTC).isoformat(),
                    outcome.result_digest,
                    outcome.failure_reason,
                    serialized.decode("utf-8"),
                    content_sha256,
                ),
            )
        return outcome

    def _consume_holdout_sync(
        self,
        consumption: HoldoutConsumption,
    ) -> HoldoutConsumption:
        serialized = canonical_json_bytes(consumption.to_document())
        content_sha256 = hashlib.sha256(serialized).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_experiment(connection, consumption.experiment_id)
            row = connection.execute(
                "SELECT 1 FROM holdout_consumptions WHERE experiment_id = ?",
                (consumption.experiment_id,),
            ).fetchone()
            if row is not None:
                raise HoldoutConsumedError(
                    "Untouched holdout has already been consumed for this experiment."
                )
            connection.execute(
                """
                INSERT INTO holdout_consumptions(
                    experiment_id,
                    consumed_at,
                    result_digest,
                    consumption_json,
                    consumption_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    consumption.experiment_id,
                    consumption.consumed_at.astimezone(UTC).isoformat(),
                    consumption.result_digest,
                    serialized.decode("utf-8"),
                    content_sha256,
                ),
            )
        return consumption

    def _get_sync(self, experiment_id: str) -> ExperimentSnapshot:
        with self._connect() as connection:
            record_row = connection.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments "
                "WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if record_row is None:
                raise ResearchNotFoundError("Research experiment does not exist.")
            outcome_row = connection.execute(
                f"SELECT {_OUTCOME_COLUMNS} FROM experiment_outcomes "
                "WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            holdout_row = connection.execute(
                f"SELECT {_HOLDOUT_COLUMNS} FROM holdout_consumptions "
                "WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return ExperimentSnapshot(
            record=self._record_from_row(record_row),
            outcome=(
                None
                if outcome_row is None
                else self._outcome_from_row(outcome_row)
            ),
            holdout_consumption=(
                None
                if holdout_row is None
                else self._holdout_consumption_from_row(holdout_row)
            ),
        )

    def _query_sync(self, limit: int) -> tuple[ExperimentSnapshot, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ResearchConfigurationError(
                "Research query limit must be between 1 and 10000."
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_EXPERIMENT_COLUMNS} FROM experiments "
                "ORDER BY registered_at ASC, experiment_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            snapshots: list[ExperimentSnapshot] = []
            for row in rows:
                record = self._record_from_row(row)
                outcome_row = connection.execute(
                    f"SELECT {_OUTCOME_COLUMNS} FROM experiment_outcomes "
                    "WHERE experiment_id = ?",
                    (record.experiment_id,),
                ).fetchone()
                holdout_row = connection.execute(
                    f"SELECT {_HOLDOUT_COLUMNS} FROM holdout_consumptions "
                    "WHERE experiment_id = ?",
                    (record.experiment_id,),
                ).fetchone()
                snapshots.append(
                    ExperimentSnapshot(
                        record=record,
                        outcome=(
                            None
                            if outcome_row is None
                            else self._outcome_from_row(outcome_row)
                        ),
                        holdout_consumption=(
                            None
                            if holdout_row is None
                            else self._holdout_consumption_from_row(holdout_row)
                        ),
                    )
                )
        return tuple(snapshots)

    def _record_from_row(self, row: Sequence[object]) -> ExperimentRecord:
        document = self._verified_document(row[4], row[5], "experiment")
        record = experiment_record_from_document(document)
        catalog_values = (
            record.experiment_id,
            record.research_digest,
            record.registered_at.astimezone(UTC).isoformat(),
            record.spec.parent_experiment_id,
        )
        if catalog_values != tuple(row[:4]):
            raise ResearchIntegrityError(
                "Stored experiment record does not match catalog metadata."
            )
        return record

    def _outcome_from_row(self, row: Sequence[object]) -> ExperimentOutcome:
        document = self._verified_document(row[5], row[6], "outcome")
        outcome = experiment_outcome_from_document(document)
        catalog_values = (
            outcome.experiment_id,
            outcome.status.value,
            outcome.completed_at.astimezone(UTC).isoformat(),
            outcome.result_digest,
            outcome.failure_reason,
        )
        if catalog_values != tuple(row[:5]):
            raise ResearchIntegrityError(
                "Stored experiment outcome does not match catalog metadata."
            )
        return outcome

    def _holdout_consumption_from_row(
        self,
        row: Sequence[object],
    ) -> HoldoutConsumption:
        document = self._verified_document(row[3], row[4], "holdout consumption")
        consumption = holdout_consumption_from_document(document)
        catalog_values = (
            consumption.experiment_id,
            consumption.consumed_at.astimezone(UTC).isoformat(),
            consumption.result_digest,
        )
        if catalog_values != tuple(row[:3]):
            raise ResearchIntegrityError(
                "Stored holdout consumption does not match catalog metadata."
            )
        return consumption

    def _verified_document(
        self,
        serialized_value: object,
        expected_sha256: object,
        label: str,
    ) -> dict[str, object]:
        if not isinstance(serialized_value, str) or not isinstance(
            expected_sha256, str
        ):
            raise ResearchIntegrityError(
                f"Stored {label} catalog row is invalid."
            )
        serialized = serialized_value.encode("utf-8")
        if hashlib.sha256(serialized).hexdigest() != expected_sha256:
            raise ResearchIntegrityError(f"Stored {label} failed SHA-256 verification.")
        try:
            document = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise ResearchIntegrityError(
                f"Stored {label} is not valid JSON."
            ) from error
        if not isinstance(document, dict):
            raise ResearchIntegrityError(f"Stored {label} is not a JSON object.")
        return cast(dict[str, object], document)

    def _require_experiment(
        self,
        connection: sqlite3.Connection,
        experiment_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise ResearchNotFoundError("Research experiment does not exist.")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    research_digest TEXT NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL,
                    parent_experiment_id TEXT,
                    spec_json TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL,
                    FOREIGN KEY(parent_experiment_id)
                        REFERENCES experiments(experiment_id)
                );

                CREATE TABLE IF NOT EXISTS experiment_outcomes (
                    experiment_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
                    completed_at TEXT NOT NULL,
                    result_digest TEXT,
                    failure_reason TEXT,
                    outcome_json TEXT NOT NULL,
                    outcome_sha256 TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiments(experiment_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS holdout_consumptions (
                    experiment_id TEXT PRIMARY KEY,
                    consumed_at TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    consumption_json TEXT NOT NULL,
                    consumption_sha256 TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiments(experiment_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_experiments_registered
                ON experiments(registered_at, experiment_id);

                CREATE INDEX IF NOT EXISTS idx_experiments_parent
                ON experiments(parent_experiment_id, registered_at, experiment_id);
                """
            )
            row = connection.execute(
                "SELECT value FROM research_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO research_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
            elif row[0] != str(_SCHEMA_VERSION):
                raise ResearchConfigurationError(
                    "Research-registry schema version is unsupported."
                )
        try:
            os.chmod(self._catalog_path, 0o600)
        except OSError as error:
            raise ResearchIntegrityError(
                "Could not secure the research-registry catalog."
            ) from error

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self._catalog_path, timeout=30.0)
        except sqlite3.Error as error:
            raise ResearchError("Could not open the research registry.") from error
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
