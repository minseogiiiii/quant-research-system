from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from world_quant_system.data.raw_market_data_models import (
    _CATALOG_SCHEMA_VERSION,
    _DEFAULT_COMPRESSION_LEVEL,
    _DEFAULT_MAX_UNCOMPRESSED_BYTES,
    _SCHEMA_VERSION,
    RawMarketDataCapture,
    RawMarketDataConfigurationError,
    RawMarketDataConflictError,
    RawMarketDataError,
    RawMarketDataIntegrityError,
    RawMarketDataMetadata,
    RawMarketDataNotFoundError,
    RawMarketDataRecord,
    RawMarketDataRecorder,
    RawMarketDataSerializationError,
    _canonical_json_bytes,
    _format_datetime,
    _normalize_json_mapping,
    _parse_datetime,
    _parse_document_datetime,
    _reject_sensitive_json,
    _require_aware_datetime,
    _require_string_dict,
    _sanitize_string_mapping,
    _validate_endpoint,
    _validate_provider,
    _validate_record_id,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - supported target platforms provide fcntl
    fcntl = None  # type: ignore[assignment]


class FileRawMarketDataStore:
    """Append-only, compressed raw response archive with a SQLite catalog.

    The compressed JSON document is written atomically before its catalog row is
    marked ready. A small pending area plus recovery logic makes interrupted
    writes repairable without scanning the full archive. Writers are serialized
    across threads and, on macOS/Linux, across processes.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        compression_level: int = _DEFAULT_COMPRESSION_LEVEL,
        max_uncompressed_bytes: int = _DEFAULT_MAX_UNCOMPRESSED_BYTES,
    ) -> None:
        self._root = Path(root).expanduser().resolve()

        if (
            isinstance(compression_level, bool)
            or not isinstance(compression_level, int)
            or not 0 <= compression_level <= 9
        ):
            raise RawMarketDataConfigurationError(
                "Compression level must be an integer between 0 and 9."
            )

        if (
            isinstance(max_uncompressed_bytes, bool)
            or not isinstance(max_uncompressed_bytes, int)
            or max_uncompressed_bytes <= 0
        ):
            raise RawMarketDataConfigurationError(
                "Maximum uncompressed size must be a positive integer."
            )

        self._compression_level = compression_level
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._blob_root = self._root / "blobs"
        self._pending_root = self._root / ".pending"
        self._catalog_path = self._root / "catalog.sqlite3"
        self._lock_path = self._root / ".writer.lock"
        self._thread_lock = threading.RLock()

        self._create_private_directory(self._root)
        self._create_private_directory(self._blob_root)
        self._create_private_directory(self._pending_root)
        self._initialize_catalog()
        self.recover_pending()

    @property
    def root(self) -> Path:
        return self._root

    async def record(
        self,
        capture: RawMarketDataCapture,
    ) -> RawMarketDataMetadata:
        if not isinstance(capture, RawMarketDataCapture):
            raise RawMarketDataConfigurationError(
                "Capture must be a RawMarketDataCapture instance."
            )

        return await asyncio.to_thread(self._record_sync, capture)

    async def read(self, record_id: str) -> RawMarketDataRecord:
        return await asyncio.to_thread(self._read_sync, record_id)

    async def verify(self, record_id: str) -> RawMarketDataMetadata:
        return await asyncio.to_thread(self._verify_sync, record_id)

    async def query(
        self,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        captured_from: datetime | None = None,
        captured_to: datetime | None = None,
        limit: int = 100,
    ) -> tuple[RawMarketDataMetadata, ...]:
        return await asyncio.to_thread(
            self._query_sync,
            provider,
            endpoint,
            captured_from,
            captured_to,
            limit,
        )

    def recover_pending(self) -> int:
        """Finish interrupted writes and return the number of recovered rows."""
        with self._writer_lock():
            return self._recover_pending_locked()

    def _record_sync(
        self,
        capture: RawMarketDataCapture,
    ) -> RawMarketDataMetadata:
        document, fingerprint = self._build_document(capture)
        uncompressed = _canonical_json_bytes(document)

        if len(uncompressed) > self._max_uncompressed_bytes:
            raise RawMarketDataSerializationError(
                "Raw market-data record exceeds the configured size limit."
            )

        with self._writer_lock():
            self._recover_pending_locked()

            existing = self._find_by_idempotency_key(capture.idempotency_key)
            if existing is not None:
                existing_fingerprint = self._catalog_fingerprint(existing.record_id)
                if existing_fingerprint != fingerprint:
                    raise RawMarketDataConflictError(
                        "Idempotency key is already associated with different content."
                    )
                self._verify_metadata_file(existing)
                return existing

            record_id = str(uuid4())
            captured_at = capture.captured_at.astimezone(UTC)
            relative_path = self._relative_blob_path(
                provider=capture.provider,
                captured_at=captured_at,
                record_id=record_id,
            )
            final_path = self._safe_archive_path(relative_path)
            pending_path = self._pending_root / f"{record_id}.json.gz"

            document["record_id"] = record_id
            final_uncompressed = _canonical_json_bytes(document)
            final_sha256 = hashlib.sha256(final_uncompressed).hexdigest()
            final_compressed = gzip.compress(
                final_uncompressed,
                compresslevel=self._compression_level,
                mtime=0,
            )

            if len(final_uncompressed) > self._max_uncompressed_bytes:
                raise RawMarketDataSerializationError(
                    "Raw market-data record exceeds the configured size limit."
                )

            self._write_atomic(pending_path, final_compressed)

            try:
                self._insert_pending_row(
                    record_id=record_id,
                    capture=capture,
                    captured_at=captured_at,
                    content_sha256=final_sha256,
                    fingerprint=fingerprint,
                    relative_path=relative_path,
                    uncompressed_size=len(final_uncompressed),
                    compressed_size=len(final_compressed),
                )
                self._create_private_directory(final_path.parent)
                os.replace(pending_path, final_path)
                self._chmod_private_file(final_path)
                self._fsync_directory(final_path.parent)
                self._mark_ready(record_id)
            except RawMarketDataConflictError:
                pending_path.unlink(missing_ok=True)
                raise
            except Exception as error:
                raise RawMarketDataError(
                    "Raw market-data write did not complete; recovery is required."
                ) from error

            metadata = self._metadata_by_id(record_id)
            self._verify_metadata_file(metadata)
            return metadata

    def _read_sync(self, record_id: str) -> RawMarketDataRecord:
        metadata = self._metadata_by_id(record_id)
        document = self._read_verified_document(metadata)

        request_params = _require_string_dict(
            document.get("request_params"),
            "request_params",
        )
        response_headers = _require_string_dict(
            document.get("response_headers"),
            "response_headers",
        )
        json_body_value = document.get("json_body")
        json_body: Mapping[str, object] | None
        if json_body_value is None:
            json_body = None
        elif isinstance(json_body_value, dict):
            json_body = cast(dict[str, object], json_body_value)
        else:
            raise RawMarketDataIntegrityError(
                "Stored json_body is not a mapping or None."
            )

        text = document.get("text")
        if not isinstance(text, str):
            raise RawMarketDataIntegrityError("Stored response text is invalid.")

        return RawMarketDataRecord(
            metadata=metadata,
            request_params=request_params,
            response_headers=response_headers,
            json_body=json_body,
            text=text,
        )

    def _verify_sync(self, record_id: str) -> RawMarketDataMetadata:
        metadata = self._metadata_by_id(record_id)
        self._read_verified_document(metadata)
        return metadata

    def _query_sync(
        self,
        provider: str | None,
        endpoint: str | None,
        captured_from: datetime | None,
        captured_to: datetime | None,
        limit: int,
    ) -> tuple[RawMarketDataMetadata, ...]:
        if provider is not None:
            _validate_provider(provider)
        if endpoint is not None:
            _validate_endpoint(endpoint)
        if captured_from is not None:
            _require_aware_datetime(captured_from, "Captured-from timestamp")
        if captured_to is not None:
            _require_aware_datetime(captured_to, "Captured-to timestamp")
        if (
            captured_from is not None
            and captured_to is not None
            and captured_from.astimezone(UTC) > captured_to.astimezone(UTC)
        ):
            raise RawMarketDataConfigurationError(
                "Captured-from timestamp cannot be later than captured-to timestamp."
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise RawMarketDataConfigurationError(
                "Query limit must be between 1 and 10000."
            )

        clauses = ["state = 'ready'"]
        values: list[object] = []

        if provider is not None:
            clauses.append("provider = ?")
            values.append(provider)
        if endpoint is not None:
            clauses.append("endpoint = ?")
            values.append(endpoint)
        if captured_from is not None:
            clauses.append("captured_at >= ?")
            values.append(_format_datetime(captured_from))
        if captured_to is not None:
            clauses.append("captured_at <= ?")
            values.append(_format_datetime(captured_to))

        values.append(limit)
        sql = f"""
            SELECT {_METADATA_COLUMNS}
            FROM raw_records
            WHERE {" AND ".join(clauses)}
            ORDER BY captured_at ASC, record_id ASC
            LIMIT ?
        """

        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()

        return tuple(self._metadata_from_row(row) for row in rows)

    def _build_document(
        self,
        capture: RawMarketDataCapture,
    ) -> tuple[dict[str, object], str]:
        request_params = _sanitize_string_mapping(capture.request_params)
        response_headers = _sanitize_string_mapping(capture.response_headers)
        json_body = _normalize_json_mapping(capture.json_body)
        _reject_sensitive_json(json_body)

        content = {
            "provider": capture.provider,
            "endpoint": capture.endpoint,
            "request_params": request_params,
            "status_code": capture.status_code,
            "response_headers": response_headers,
            "json_body": json_body,
            "text": capture.text,
            "request_id": capture.request_id,
            "schema_version": _SCHEMA_VERSION,
        }
        fingerprint = hashlib.sha256(_canonical_json_bytes(content)).hexdigest()

        document = {
            **content,
            "record_id": None,
            "captured_at": _format_datetime(capture.captured_at),
            "idempotency_key": capture.idempotency_key,
        }
        return document, fingerprint

    def _initialize_catalog(self) -> None:
        with self._writer_lock(), self._connect() as connection:
            connection.executescript(
                """
                    CREATE TABLE IF NOT EXISTS catalog_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS raw_records (
                        record_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        status_code INTEGER NOT NULL,
                        request_id TEXT,
                        idempotency_key TEXT UNIQUE,
                        content_sha256 TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        relative_path TEXT NOT NULL UNIQUE,
                        uncompressed_size INTEGER NOT NULL,
                        compressed_size INTEGER NOT NULL,
                        schema_version INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'ready'))
                    );

                    CREATE INDEX IF NOT EXISTS idx_raw_records_capture
                    ON raw_records(provider, endpoint, captured_at, record_id);
                    """
            )
            row = connection.execute(
                "SELECT value FROM catalog_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO catalog_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_CATALOG_SCHEMA_VERSION)),
                )
            elif row[0] != str(_CATALOG_SCHEMA_VERSION):
                raise RawMarketDataConfigurationError(
                    "Raw market-data catalog schema version is unsupported."
                )

        self._chmod_private_file(self._catalog_path)

    def _insert_pending_row(
        self,
        *,
        record_id: str,
        capture: RawMarketDataCapture,
        captured_at: datetime,
        content_sha256: str,
        fingerprint: str,
        relative_path: str,
        uncompressed_size: int,
        compressed_size: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO raw_records(
                        record_id,
                        provider,
                        endpoint,
                        captured_at,
                        status_code,
                        request_id,
                        idempotency_key,
                        content_sha256,
                        fingerprint,
                        relative_path,
                        uncompressed_size,
                        compressed_size,
                        schema_version,
                        state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        record_id,
                        capture.provider,
                        capture.endpoint,
                        _format_datetime(captured_at),
                        capture.status_code,
                        capture.request_id,
                        capture.idempotency_key,
                        content_sha256,
                        fingerprint,
                        relative_path,
                        uncompressed_size,
                        compressed_size,
                        _SCHEMA_VERSION,
                    ),
                )
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise RawMarketDataConflictError(
                    "Raw market-data catalog rejected a duplicate record."
                ) from error
            else:
                connection.commit()

    def _mark_ready(self, record_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE raw_records SET state = 'ready' WHERE record_id = ?",
                (record_id,),
            )
            if cursor.rowcount != 1:
                raise RawMarketDataIntegrityError(
                    "Pending raw record disappeared before commit."
                )

    def _recover_pending_locked(self) -> int:
        recovered = 0

        with self._connect() as connection:
            pending_rows = connection.execute(
                f"""
                SELECT {_METADATA_COLUMNS}, fingerprint
                FROM raw_records
                WHERE state = 'pending'
                ORDER BY captured_at, record_id
                """
            ).fetchall()

        known_pending_ids: set[str] = set()
        for row in pending_rows:
            metadata = self._metadata_from_row(row[:13])
            known_pending_ids.add(metadata.record_id)
            pending_path = self._pending_root / f"{metadata.record_id}.json.gz"
            final_path = self._safe_archive_path(metadata.relative_path)

            if final_path.exists():
                self._verify_metadata_file(metadata)
                if pending_path.exists():
                    _, pending_bytes, pending_sha256 = self._decode_file(pending_path)
                    if (
                        pending_sha256 != metadata.content_sha256
                        or len(pending_bytes) != metadata.uncompressed_size
                    ):
                        raise RawMarketDataIntegrityError(
                            "Pending duplicate does not match the committed file."
                        )
                    pending_path.unlink()
            elif pending_path.exists():
                self._create_private_directory(final_path.parent)
                os.replace(pending_path, final_path)
                self._chmod_private_file(final_path)
                self._fsync_directory(final_path.parent)
                self._verify_metadata_file(metadata)
            else:
                raise RawMarketDataIntegrityError(
                    "Pending catalog row has no corresponding data file."
                )

            self._mark_ready(metadata.record_id)
            recovered += 1

        for pending_path in self._pending_root.glob("*.json.gz"):
            record_id = pending_path.name.removesuffix(".json.gz")
            if record_id in known_pending_ids:
                continue

            document, uncompressed, content_sha256 = self._decode_file(pending_path)
            metadata, fingerprint = self._metadata_from_document(
                document,
                relative_path=self._relative_path_from_document(document),
                content_sha256=content_sha256,
                uncompressed_size=len(uncompressed),
                compressed_size=pending_path.stat().st_size,
            )
            final_path = self._safe_archive_path(metadata.relative_path)
            self._insert_recovered_pending(metadata, fingerprint)
            self._create_private_directory(final_path.parent)
            os.replace(pending_path, final_path)
            self._chmod_private_file(final_path)
            self._fsync_directory(final_path.parent)
            self._mark_ready(metadata.record_id)
            recovered += 1

        return recovered

    def _insert_recovered_pending(
        self,
        metadata: RawMarketDataMetadata,
        fingerprint: str,
    ) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO raw_records(
                        record_id,
                        provider,
                        endpoint,
                        captured_at,
                        status_code,
                        request_id,
                        idempotency_key,
                        content_sha256,
                        fingerprint,
                        relative_path,
                        uncompressed_size,
                        compressed_size,
                        schema_version,
                        state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        metadata.record_id,
                        metadata.provider,
                        metadata.endpoint,
                        _format_datetime(metadata.captured_at),
                        metadata.status_code,
                        metadata.request_id,
                        metadata.idempotency_key,
                        metadata.content_sha256,
                        fingerprint,
                        metadata.relative_path,
                        metadata.uncompressed_size,
                        metadata.compressed_size,
                        metadata.schema_version,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise RawMarketDataConflictError(
                    "Recovered raw record conflicts with the catalog."
                ) from error

    def _relative_path_from_document(self, document: Mapping[str, object]) -> str:
        provider = document.get("provider")
        captured_at = _parse_document_datetime(document.get("captured_at"))
        record_id = document.get("record_id")
        if not isinstance(provider, str) or not isinstance(record_id, str):
            raise RawMarketDataIntegrityError("Pending raw record lacks path metadata.")
        _validate_provider(provider)
        _validate_record_id(record_id)
        return self._relative_blob_path(provider, captured_at, record_id)

    def _metadata_from_document(
        self,
        document: Mapping[str, object],
        *,
        relative_path: str,
        content_sha256: str,
        uncompressed_size: int,
        compressed_size: int,
    ) -> tuple[RawMarketDataMetadata, str]:
        record_id = document.get("record_id")
        provider = document.get("provider")
        endpoint = document.get("endpoint")
        status_code = document.get("status_code")
        request_id = document.get("request_id")
        idempotency_key = document.get("idempotency_key")
        schema_version = document.get("schema_version")

        if not isinstance(record_id, str):
            raise RawMarketDataIntegrityError("Stored record ID is invalid.")
        if not isinstance(provider, str):
            raise RawMarketDataIntegrityError("Stored provider is invalid.")
        if not isinstance(endpoint, str):
            raise RawMarketDataIntegrityError("Stored endpoint is invalid.")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise RawMarketDataIntegrityError("Stored status code is invalid.")
        if request_id is not None and not isinstance(request_id, str):
            raise RawMarketDataIntegrityError("Stored request ID is invalid.")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise RawMarketDataIntegrityError("Stored idempotency key is invalid.")
        if not isinstance(schema_version, int):
            raise RawMarketDataIntegrityError("Stored schema version is invalid.")

        content = {
            "provider": provider,
            "endpoint": endpoint,
            "request_params": document.get("request_params"),
            "status_code": status_code,
            "response_headers": document.get("response_headers"),
            "json_body": document.get("json_body"),
            "text": document.get("text"),
            "request_id": request_id,
            "schema_version": schema_version,
        }
        fingerprint = hashlib.sha256(_canonical_json_bytes(content)).hexdigest()

        return (
            RawMarketDataMetadata(
                record_id=record_id,
                provider=provider,
                endpoint=endpoint,
                captured_at=_parse_document_datetime(document.get("captured_at")),
                status_code=status_code,
                request_id=request_id,
                idempotency_key=idempotency_key,
                content_sha256=content_sha256,
                relative_path=relative_path,
                uncompressed_size=uncompressed_size,
                compressed_size=compressed_size,
                schema_version=schema_version,
            ),
            fingerprint,
        )

    def _find_by_idempotency_key(
        self,
        idempotency_key: str | None,
    ) -> RawMarketDataMetadata | None:
        if idempotency_key is None:
            return None

        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {_METADATA_COLUMNS}
                FROM raw_records
                WHERE idempotency_key = ? AND state = 'ready'
                """,
                (idempotency_key,),
            ).fetchone()

        return None if row is None else self._metadata_from_row(row)

    def _catalog_fingerprint(self, record_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM raw_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()

        if row is None or not isinstance(row[0], str):
            raise RawMarketDataIntegrityError("Catalog fingerprint is missing.")
        return row[0]

    def _metadata_by_id(self, record_id: str) -> RawMarketDataMetadata:
        _validate_record_id(record_id)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {_METADATA_COLUMNS}
                FROM raw_records
                WHERE record_id = ? AND state = 'ready'
                """,
                (record_id,),
            ).fetchone()

        if row is None:
            raise RawMarketDataNotFoundError(
                f"Raw market-data record was not found: {record_id}."
            )
        return self._metadata_from_row(row)

    def _metadata_from_row(self, row: Sequence[object]) -> RawMarketDataMetadata:
        return RawMarketDataMetadata(
            record_id=cast(str, row[0]),
            provider=cast(str, row[1]),
            endpoint=cast(str, row[2]),
            captured_at=_parse_datetime(cast(str, row[3])),
            status_code=cast(int, row[4]),
            request_id=cast(str | None, row[5]),
            idempotency_key=cast(str | None, row[6]),
            content_sha256=cast(str, row[7]),
            relative_path=cast(str, row[8]),
            uncompressed_size=cast(int, row[9]),
            compressed_size=cast(int, row[10]),
            schema_version=cast(int, row[11]),
        )

    def _read_verified_document(
        self,
        metadata: RawMarketDataMetadata,
    ) -> dict[str, object]:
        path = self._safe_archive_path(metadata.relative_path)
        document, uncompressed, content_sha256 = self._decode_file(path)

        if content_sha256 != metadata.content_sha256:
            raise RawMarketDataIntegrityError(
                "Raw market-data checksum does not match the catalog."
            )
        if len(uncompressed) != metadata.uncompressed_size:
            raise RawMarketDataIntegrityError(
                "Raw market-data uncompressed size does not match the catalog."
            )
        if path.stat().st_size != metadata.compressed_size:
            raise RawMarketDataIntegrityError(
                "Raw market-data compressed size does not match the catalog."
            )
        if document.get("record_id") != metadata.record_id:
            raise RawMarketDataIntegrityError(
                "Raw market-data file belongs to a different record."
            )

        expected_fields: tuple[tuple[str, object], ...] = (
            ("provider", metadata.provider),
            ("endpoint", metadata.endpoint),
            ("captured_at", _format_datetime(metadata.captured_at)),
            ("status_code", metadata.status_code),
            ("request_id", metadata.request_id),
            ("idempotency_key", metadata.idempotency_key),
            ("schema_version", metadata.schema_version),
        )
        for field_name, expected_value in expected_fields:
            if document.get(field_name) != expected_value:
                raise RawMarketDataIntegrityError(
                    f"Raw market-data {field_name} does not match the catalog."
                )

        expected_path = self._relative_path_from_document(document)
        if expected_path != metadata.relative_path:
            raise RawMarketDataIntegrityError(
                "Raw market-data path does not match document metadata."
            )

        return document

    def _verify_metadata_file(self, metadata: RawMarketDataMetadata) -> None:
        self._read_verified_document(metadata)

    def _decode_file(
        self,
        path: Path,
    ) -> tuple[dict[str, object], bytes, str]:
        if not path.is_file():
            raise RawMarketDataIntegrityError(
                f"Raw market-data file is missing: {path.name}."
            )

        try:
            compressed = path.read_bytes()
            uncompressed = gzip.decompress(compressed)
        except (OSError, EOFError) as error:
            raise RawMarketDataIntegrityError(
                "Raw market-data file is not valid gzip content."
            ) from error

        if len(uncompressed) > self._max_uncompressed_bytes:
            raise RawMarketDataIntegrityError(
                "Raw market-data file exceeds the configured size limit."
            )

        try:
            loaded = json.loads(uncompressed)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RawMarketDataIntegrityError(
                "Raw market-data file does not contain valid JSON."
            ) from error

        if not isinstance(loaded, dict):
            raise RawMarketDataIntegrityError(
                "Raw market-data document must be a JSON object."
            )

        document = cast(dict[str, object], loaded)
        return document, uncompressed, hashlib.sha256(uncompressed).hexdigest()

    def _relative_blob_path(
        self,
        provider: str,
        captured_at: datetime,
        record_id: str,
    ) -> str:
        utc_time = captured_at.astimezone(UTC)
        return f"blobs/{provider}/{utc_time:%Y/%m/%d}/{record_id}.json.gz"

    def _safe_archive_path(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise RawMarketDataIntegrityError(
                "Catalog path escapes the raw storage root."
            ) from error
        return candidate

    def _write_atomic(self, destination: Path, content: bytes) -> None:
        self._create_private_directory(destination.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)

        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
            self._chmod_private_file(destination)
            self._fsync_directory(destination.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._catalog_path,
            timeout=30.0,
        )
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        with self._thread_lock:
            self._lock_path.touch(mode=0o600, exist_ok=True)
            self._chmod_private_file(self._lock_path)
            with self._lock_path.open("a+b") as lock_stream:
                if fcntl is not None:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _create_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            os.chmod(path, 0o700)

    @staticmethod
    def _chmod_private_file(path: Path) -> None:
        if not path.exists():
            return
        with suppress(OSError):
            os.chmod(path, 0o600)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


_METADATA_COLUMNS = """
record_id,
provider,
endpoint,
captured_at,
status_code,
request_id,
idempotency_key,
content_sha256,
relative_path,
uncompressed_size,
compressed_size,
schema_version
""".strip()


__all__ = [
    "FileRawMarketDataStore",
    "RawMarketDataCapture",
    "RawMarketDataConfigurationError",
    "RawMarketDataConflictError",
    "RawMarketDataError",
    "RawMarketDataIntegrityError",
    "RawMarketDataMetadata",
    "RawMarketDataNotFoundError",
    "RawMarketDataRecord",
    "RawMarketDataRecorder",
    "RawMarketDataSerializationError",
]
