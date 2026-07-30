from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from world_quant_system.data import (
    FileRawMarketDataStore,
    NormalizedCandleCursor,
    SQLiteDataQualityStore,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.domain import CandleInterval
from world_quant_system.research.historical_dataset_models import (
    BenchmarkLink,
    HistoricalDatasetConflictError,
    HistoricalDatasetEligibilityError,
    HistoricalDatasetFrozenError,
    HistoricalDatasetIntegrityError,
    HistoricalDatasetManifest,
    HistoricalDatasetNotFoundError,
    HistoricalDatasetSnapshot,
    HistoricalDatasetState,
    canonical_manifest_json,
    historical_dataset_manifest_from_document,
)

_SCHEMA_VERSION = 1
_DATASET_COLUMNS = """
    dataset_id,
    manifest_path,
    manifest_sha256,
    dataset_digest,
    state,
    frozen_at,
    imported_at,
    provider,
    exchange,
    symbol,
    interval,
    start_at,
    end_at
"""
_LINK_COLUMNS = """
    link_id,
    dataset_id,
    benchmark_dataset_id,
    alignment_digest,
    created_at
"""


class SQLiteHistoricalDatasetStore:
    """Immutable historical-dataset manifests with deterministic validation."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._manifests = self._root / "manifests"
        self._datasets = self._root / "datasets"
        self._manifests.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._datasets.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._database = self._root / "historical_datasets.sqlite3"
        self._secure_directory(self._root)
        self._secure_directory(self._manifests)
        self._secure_directory(self._datasets)
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    def dataset_root(self, dataset_id: str) -> Path:
        return self._datasets / dataset_id

    async def save_manifest(
        self,
        manifest: HistoricalDatasetManifest,
    ) -> HistoricalDatasetSnapshot:
        return await asyncio.to_thread(self._save_manifest_sync, manifest)

    async def get_snapshot(self, dataset_id: str) -> HistoricalDatasetSnapshot:
        return await asyncio.to_thread(self._get_snapshot_sync, dataset_id)

    async def freeze(
        self,
        dataset_id: str,
        *,
        frozen_at: datetime | None = None,
    ) -> HistoricalDatasetSnapshot:
        snapshot = await self.get_snapshot(dataset_id)
        resolved = frozen_at or (
            snapshot.manifest.imported_at.astimezone(UTC) + timedelta(microseconds=1)
        )
        if resolved.tzinfo is None or resolved.utcoffset() is None:
            raise HistoricalDatasetIntegrityError(
                "Frozen-at timestamp must include timezone information."
            )
        return await asyncio.to_thread(
            self._freeze_sync,
            dataset_id,
            resolved.astimezone(UTC),
        )

    async def verify(self, dataset_id: str) -> HistoricalDatasetSnapshot:
        snapshot = await self.get_snapshot(dataset_id)
        manifest = snapshot.manifest
        dataset_root = self.dataset_root(dataset_id)

        raw_store = FileRawMarketDataStore(dataset_root / "raw")
        raw = await raw_store.read(manifest.raw_record_id)
        if raw.metadata.content_sha256 != manifest.raw_content_sha256:
            raise HistoricalDatasetIntegrityError(
                "Raw record digest does not match the dataset manifest."
            )
        source_bytes = raw.text.encode("utf-8")
        if hashlib.sha256(source_bytes).hexdigest() != manifest.source_sha256:
            raise HistoricalDatasetIntegrityError(
                "Raw source bytes do not match the dataset manifest."
            )

        quality_store = SQLiteDataQualityStore(dataset_root / "quality")
        report = await quality_store.get(manifest.quality_report_id)
        if (
            report.raw_content_sha256 != manifest.raw_content_sha256
            or report.status is not manifest.quality_status
            or report.policy_fingerprint != manifest.quality_policy_digest
            or report.item_count != manifest.item_count
        ):
            raise HistoricalDatasetIntegrityError(
                "Quality report does not match the dataset manifest."
            )

        normalized_store = SQLiteNormalizedMarketDataStore(
            dataset_root / "normalized"
        )
        digest, count = await _normalized_digest(
            normalized_store,
            symbol=manifest.symbols[0],
            interval=manifest.interval,
        )
        if count != manifest.item_count or digest != manifest.normalized_digest:
            raise HistoricalDatasetIntegrityError(
                "Normalized market data does not match the dataset manifest."
            )
        return snapshot

    async def link_benchmark(
        self,
        dataset_id: str,
        benchmark_dataset_id: str,
        *,
        created_at: datetime | None = None,
    ) -> BenchmarkLink:
        dataset = await self.verify(dataset_id)
        benchmark = await self.verify(benchmark_dataset_id)
        if (
            dataset.state is not HistoricalDatasetState.FROZEN
            or benchmark.state is not HistoricalDatasetState.FROZEN
        ):
            raise HistoricalDatasetFrozenError(
                "Both strategy and benchmark datasets must be frozen before linking."
            )
        resolved = created_at or max(
            cast(datetime, dataset.frozen_at),
            cast(datetime, benchmark.frozen_at),
        ) + timedelta(microseconds=1)
        dataset_times = await _normalized_event_times(
            SQLiteNormalizedMarketDataStore(
                self.dataset_root(dataset_id) / "normalized"
            ),
            symbol=dataset.manifest.symbols[0],
            interval=dataset.manifest.interval,
        )
        benchmark_times = await _normalized_event_times(
            SQLiteNormalizedMarketDataStore(
                self.dataset_root(benchmark_dataset_id) / "normalized"
            ),
            symbol=benchmark.manifest.symbols[0],
            interval=benchmark.manifest.interval,
        )
        if dataset_times != benchmark_times:
            raise HistoricalDatasetEligibilityError(
                "Benchmark sessions must align exactly with the strategy dataset."
            )
        link = BenchmarkLink.build(
            dataset=dataset.manifest,
            benchmark=benchmark.manifest,
            created_at=resolved,
        )
        return await asyncio.to_thread(self._save_link_sync, link)

    async def get_benchmark_link(self, dataset_id: str) -> BenchmarkLink:
        return await asyncio.to_thread(self._get_link_sync, dataset_id)

    async def list_snapshots(
        self,
        *,
        limit: int = 1_000,
    ) -> tuple[HistoricalDatasetSnapshot, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise HistoricalDatasetIntegrityError(
                "Dataset query limit must be between 1 and 10000."
            )
        ids = await asyncio.to_thread(self._list_ids_sync, limit)
        snapshots: list[HistoricalDatasetSnapshot] = []
        for dataset_id in ids:
            snapshots.append(await self.get_snapshot(dataset_id))
        return tuple(snapshots)

    def _save_manifest_sync(
        self,
        manifest: HistoricalDatasetManifest,
    ) -> HistoricalDatasetSnapshot:
        if not isinstance(manifest, HistoricalDatasetManifest):
            raise HistoricalDatasetIntegrityError(
                "Historical dataset store accepts only manifests."
            )
        serialized = canonical_manifest_json(manifest)
        manifest_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        relative_path = f"manifests/{manifest.dataset_id}.json"
        destination = self._root / relative_path

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_DATASET_COLUMNS} FROM datasets WHERE dataset_id = ?",
                (manifest.dataset_id,),
            ).fetchone()
            if row is not None:
                existing = self._snapshot_from_row(row)
                if existing.dataset_digest != manifest.dataset_digest:
                    raise HistoricalDatasetConflictError(
                        "Dataset ID already exists with different immutable data."
                    )
                return existing
            digest_row = connection.execute(
                "SELECT dataset_id FROM datasets WHERE dataset_digest = ?",
                (manifest.dataset_digest,),
            ).fetchone()
            if digest_row is not None:
                raise HistoricalDatasetConflictError(
                    "Dataset digest already belongs to another dataset ID."
                )

            self._write_atomic(destination, serialized.encode("utf-8"))
            try:
                connection.execute(
                    """
                    INSERT INTO datasets(
                        dataset_id,
                        manifest_path,
                        manifest_sha256,
                        dataset_digest,
                        state,
                        frozen_at,
                        imported_at,
                        provider,
                        exchange,
                        symbol,
                        interval,
                        start_at,
                        end_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.dataset_id,
                        relative_path,
                        manifest_sha256,
                        manifest.dataset_digest,
                        HistoricalDatasetState.IMPORTED.value,
                        format_utc(manifest.imported_at),
                        manifest.provider,
                        manifest.exchange,
                        manifest.symbols[0],
                        manifest.interval.value,
                        format_utc(manifest.start),
                        format_utc(manifest.end),
                    ),
                )
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        return HistoricalDatasetSnapshot(
            manifest=manifest,
            state=HistoricalDatasetState.IMPORTED,
            frozen_at=None,
        )

    def _get_snapshot_sync(self, dataset_id: str) -> HistoricalDatasetSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_DATASET_COLUMNS} FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise HistoricalDatasetNotFoundError(
                "Historical dataset does not exist."
            )
        return self._snapshot_from_row(row)

    def _freeze_sync(
        self,
        dataset_id: str,
        frozen_at: datetime,
    ) -> HistoricalDatasetSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_DATASET_COLUMNS} FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
            if row is None:
                raise HistoricalDatasetNotFoundError(
                    "Historical dataset does not exist."
                )
            snapshot = self._snapshot_from_row(row)
            if snapshot.state is HistoricalDatasetState.FROZEN:
                return snapshot
            connection.execute(
                "UPDATE datasets SET state = ?, frozen_at = ? WHERE dataset_id = ?",
                (
                    HistoricalDatasetState.FROZEN.value,
                    format_utc(frozen_at),
                    dataset_id,
                ),
            )
        return HistoricalDatasetSnapshot(
            manifest=snapshot.manifest,
            state=HistoricalDatasetState.FROZEN,
            frozen_at=frozen_at,
        )

    def _save_link_sync(self, link: BenchmarkLink) -> BenchmarkLink:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_LINK_COLUMNS} FROM benchmark_links WHERE dataset_id = ?",
                (link.dataset_id,),
            ).fetchone()
            if row is not None:
                existing = self._link_from_row(row)
                if existing != link:
                    raise HistoricalDatasetConflictError(
                        "Dataset already has a different benchmark link."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO benchmark_links(
                    link_id,
                    dataset_id,
                    benchmark_dataset_id,
                    alignment_digest,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    link.link_id,
                    link.dataset_id,
                    link.benchmark_dataset_id,
                    link.alignment_digest,
                    format_utc(link.created_at),
                ),
            )
        return link

    def _get_link_sync(self, dataset_id: str) -> BenchmarkLink:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_LINK_COLUMNS} FROM benchmark_links WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise HistoricalDatasetNotFoundError(
                "Benchmark link does not exist."
            )
        return self._link_from_row(row)

    def _list_ids_sync(self, limit: int) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT dataset_id FROM datasets "
                "ORDER BY imported_at ASC, dataset_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(cast(str, row[0]) for row in rows)

    def _snapshot_from_row(self, row: Sequence[object]) -> HistoricalDatasetSnapshot:
        if len(row) != 13:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset database row has an invalid shape."
            )
        (
            dataset_id,
            manifest_path,
            expected_manifest_sha256,
            expected_dataset_digest,
            state_value,
            frozen_at_value,
            imported_at,
            provider,
            exchange,
            symbol,
            interval,
            start_at,
            end_at,
        ) = row
        if not all(
            isinstance(value, str)
            for value in (
                dataset_id,
                manifest_path,
                expected_manifest_sha256,
                expected_dataset_digest,
                state_value,
                imported_at,
                provider,
                exchange,
                symbol,
                interval,
                start_at,
                end_at,
            )
        ):
            raise HistoricalDatasetIntegrityError(
                "Historical dataset catalog metadata has invalid types."
            )
        path = self._safe_manifest_path(cast(str, manifest_path))
        try:
            serialized = path.read_bytes()
        except OSError as error:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset manifest file is missing or unreadable."
            ) from error
        if hashlib.sha256(serialized).hexdigest() != expected_manifest_sha256:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset manifest failed SHA-256 verification."
            )
        try:
            document = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset manifest is not valid JSON."
            ) from error
        manifest = historical_dataset_manifest_from_document(document)
        if (
            manifest.dataset_id != dataset_id
            or manifest.dataset_digest != expected_dataset_digest
            or format_utc(manifest.imported_at) != imported_at
            or manifest.provider != provider
            or manifest.exchange != exchange
            or manifest.symbols != (symbol,)
            or manifest.interval.value != interval
            or format_utc(manifest.start) != start_at
            or format_utc(manifest.end) != end_at
        ):
            raise HistoricalDatasetIntegrityError(
                "Historical dataset manifest does not match catalog metadata."
            )
        try:
            state = HistoricalDatasetState(cast(str, state_value))
        except ValueError as error:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset state is invalid."
            ) from error
        frozen_at: datetime | None
        if frozen_at_value is None:
            frozen_at = None
        elif isinstance(frozen_at_value, str):
            frozen_at = _parse_utc(frozen_at_value)
        else:
            raise HistoricalDatasetIntegrityError(
                "Frozen-at timestamp has an invalid type."
            )
        return HistoricalDatasetSnapshot(
            manifest=manifest,
            state=state,
            frozen_at=frozen_at,
        )

    def _link_from_row(self, row: Sequence[object]) -> BenchmarkLink:
        if len(row) != 5 or not all(isinstance(value, str) for value in row):
            raise HistoricalDatasetIntegrityError(
                "Benchmark link database row is invalid."
            )
        return BenchmarkLink(
            link_id=cast(str, row[0]),
            dataset_id=cast(str, row[1]),
            benchmark_dataset_id=cast(str, row[2]),
            alignment_digest=cast(str, row[3]),
            created_at=_parse_utc(cast(str, row[4])),
        )

    def _safe_manifest_path(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve()
        try:
            candidate.relative_to(self._manifests)
        except ValueError as error:
            raise HistoricalDatasetIntegrityError(
                "Historical dataset manifest path escapes the catalog root."
            ) from error
        return candidate

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dataset_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS datasets(
                    dataset_id TEXT PRIMARY KEY,
                    manifest_path TEXT NOT NULL UNIQUE,
                    manifest_sha256 TEXT NOT NULL,
                    dataset_digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('imported', 'frozen')),
                    frozen_at TEXT,
                    imported_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS benchmark_links(
                    link_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL UNIQUE,
                    benchmark_dataset_id TEXT NOT NULL,
                    alignment_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id),
                    FOREIGN KEY(benchmark_dataset_id) REFERENCES datasets(dataset_id)
                );

                CREATE INDEX IF NOT EXISTS idx_datasets_time
                ON datasets(exchange, symbol, interval, start_at, end_at);
                """
            )
            row = connection.execute(
                "SELECT value FROM dataset_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO dataset_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
            elif row[0] != str(_SCHEMA_VERSION):
                raise HistoricalDatasetIntegrityError(
                    "Historical dataset catalog schema is unsupported."
                )
        try:
            os.chmod(self._database, 0o600)
        except OSError as error:
            raise HistoricalDatasetIntegrityError(
                "Could not secure the historical dataset catalog."
            ) from error

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _secure_directory(path: Path) -> None:
        try:
            os.chmod(path, 0o700)
        except OSError as error:
            raise HistoricalDatasetIntegrityError(
                "Could not secure a historical dataset directory."
            ) from error


async def _normalized_digest(
    store: SQLiteNormalizedMarketDataStore,
    *,
    symbol: str,
    interval: CandleInterval,
) -> tuple[str, int]:
    documents: list[dict[str, object]] = []
    cursor: NormalizedCandleCursor | None = None
    while True:
        records = await store.query_candles(
            symbols=(symbol,),
            interval=interval,
            after=cursor,
            limit=10_000,
        )
        if not records:
            break
        for record in records:
            documents.append(
                {
                    "item_id": record.item_id,
                    "content_sha256": record.content_sha256,
                    "timestamp": format_utc(record.candle.timestamp),
                    "quality_status": record.quality_status.value,
                    "lineage_count": record.lineage_count,
                }
            )
        last = records[-1]
        cursor = NormalizedCandleCursor(
            timestamp=last.candle.timestamp,
            symbol=last.candle.symbol,
            item_id=last.item_id,
        )
        if len(records) < 10_000:
            break
    return hashlib.sha256(canonical_json_bytes(documents)).hexdigest(), len(documents)


async def _normalized_event_times(
    store: SQLiteNormalizedMarketDataStore,
    *,
    symbol: str,
    interval: CandleInterval,
) -> tuple[str, ...]:
    timestamps: list[str] = []
    cursor: NormalizedCandleCursor | None = None
    while True:
        records = await store.query_candles(
            symbols=(symbol,),
            interval=interval,
            after=cursor,
            limit=10_000,
        )
        if not records:
            break
        timestamps.extend(format_utc(record.candle.timestamp) for record in records)
        last = records[-1]
        cursor = NormalizedCandleCursor(
            timestamp=last.candle.timestamp,
            symbol=last.candle.symbol,
            item_id=last.item_id,
        )
        if len(records) < 10_000:
            break
    return tuple(timestamps)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalDatasetIntegrityError(
            "Stored historical dataset timestamp is timezone-naive."
        )
    return parsed.astimezone(UTC)


__all__ = ["SQLiteHistoricalDatasetStore"]
