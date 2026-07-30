from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from world_quant_system.data.normalized_models import (
    NormalizedCandleCursor,
    NormalizedCandleRecord,
    NormalizedMarketDataReader,
    canonical_json_bytes,
)
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.domain.models import CandleInterval
from world_quant_system.research.point_in_time_models import (
    DataAvailabilityRecord,
    DelistingRecord,
    PointInTimeAccessGrant,
    PointInTimeBacktestContext,
    PointInTimeConflictError,
    PointInTimeDataKind,
    PointInTimeEligibilityError,
    PointInTimeIntegrityError,
    PointInTimeMember,
    PointInTimeNotFoundError,
    PointInTimeOverlapError,
    PointInTimePolicy,
    PointInTimeSnapshot,
    SecurityLifecycle,
    UniverseMembership,
    data_availability_from_document,
    delisting_record_from_document,
    security_lifecycle_from_document,
    universe_membership_from_document,
)

_LIFECYCLE_COLUMNS = (
    "exchange, symbol, lifecycle_id, lifecycle_json, lifecycle_sha256"
)
_MEMBERSHIP_COLUMNS = (
    "membership_id, universe_id, exchange, symbol, member_from, member_until, "
    "available_at, membership_json, membership_sha256"
)
_AVAILABILITY_COLUMNS = (
    "data_id, availability_id, data_kind, exchange, symbol, effective_at, "
    "available_at, availability_json, availability_sha256"
)
_DELISTING_COLUMNS = (
    "exchange, symbol, delisting_id, delisted_at, delisting_json, delisting_sha256"
)


class SQLitePointInTimeStore:
    """Durable, immutable point-in-time metadata and eligibility checks."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise PointInTimeIntegrityError(
                "Point-in-time storage root must be a directory."
            )
        self._database = self._root / "point_in_time.sqlite3"
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database(self) -> Path:
        return self._database

    async def save_security(self, lifecycle: SecurityLifecycle) -> SecurityLifecycle:
        if not isinstance(lifecycle, SecurityLifecycle):
            raise PointInTimeIntegrityError(
                "Point-in-time storage requires a SecurityLifecycle."
            )
        return await asyncio.to_thread(self._save_security_sync, lifecycle)

    async def save_membership(
        self,
        membership: UniverseMembership,
    ) -> UniverseMembership:
        if not isinstance(membership, UniverseMembership):
            raise PointInTimeIntegrityError(
                "Point-in-time storage requires a UniverseMembership."
            )
        return await asyncio.to_thread(self._save_membership_sync, membership)

    async def save_availability(
        self,
        availability: DataAvailabilityRecord,
    ) -> DataAvailabilityRecord:
        if not isinstance(availability, DataAvailabilityRecord):
            raise PointInTimeIntegrityError(
                "Point-in-time storage requires a DataAvailabilityRecord."
            )
        return await asyncio.to_thread(self._save_availability_sync, availability)

    async def save_delisting(self, delisting: DelistingRecord) -> DelistingRecord:
        if not isinstance(delisting, DelistingRecord):
            raise PointInTimeIntegrityError(
                "Point-in-time storage requires a DelistingRecord."
            )
        return await asyncio.to_thread(self._save_delisting_sync, delisting)

    async def get_security(self, exchange: str, symbol: str) -> SecurityLifecycle:
        return await asyncio.to_thread(self._get_security_sync, exchange, symbol)

    async def get_availability(self, data_id: str) -> DataAvailabilityRecord:
        return await asyncio.to_thread(self._get_availability_sync, data_id)

    async def get_delisting(self, exchange: str, symbol: str) -> DelistingRecord:
        return await asyncio.to_thread(self._get_delisting_sync, exchange, symbol)

    async def build_snapshot(
        self,
        universe_id: str,
        as_of: datetime,
        *,
        policy: PointInTimePolicy | None = None,
    ) -> PointInTimeSnapshot:
        return await asyncio.to_thread(
            self._build_snapshot_sync,
            universe_id,
            as_of,
            policy or PointInTimePolicy(),
        )

    async def build_backtest_context(
        self,
        *,
        universe_id: str,
        exchange: str,
        symbol: str,
        start: datetime,
        end: datetime,
        policy: PointInTimePolicy | None = None,
    ) -> PointInTimeBacktestContext:
        return await asyncio.to_thread(
            self._build_backtest_context_sync,
            universe_id,
            exchange,
            symbol,
            start,
            end,
            policy or PointInTimePolicy(),
        )

    async def validate_access(
        self,
        *,
        universe_id: str,
        exchange: str,
        symbol: str,
        data_id: str,
        event_at: datetime,
        decision_at: datetime,
        policy: PointInTimePolicy | None = None,
    ) -> PointInTimeAccessGrant:
        return await asyncio.to_thread(
            self._validate_access_sync,
            universe_id,
            exchange,
            symbol,
            data_id,
            event_at,
            decision_at,
            policy or PointInTimePolicy(),
        )

    def _save_security_sync(self, lifecycle: SecurityLifecycle) -> SecurityLifecycle:
        serialized, content_sha256 = _serialized(lifecycle.to_document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_LIFECYCLE_COLUMNS} FROM security_lifecycles "
                "WHERE exchange = ? AND symbol = ?",
                (lifecycle.exchange, lifecycle.symbol),
            ).fetchone()
            if row is not None:
                existing = self._lifecycle_from_row(row)
                if existing != lifecycle:
                    raise PointInTimeConflictError(
                        "Security lifecycle is immutable and already differs."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO security_lifecycles(
                    exchange,
                    symbol,
                    lifecycle_id,
                    listed_at,
                    delisted_at,
                    tradable_from,
                    tradable_until,
                    lifecycle_json,
                    lifecycle_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lifecycle.exchange,
                    lifecycle.symbol,
                    lifecycle.lifecycle_id,
                    _iso(lifecycle.listed_at),
                    _optional_iso(lifecycle.delisted_at),
                    _iso(lifecycle.tradable_from),
                    _optional_iso(lifecycle.tradable_until),
                    serialized,
                    content_sha256,
                ),
            )
        return lifecycle

    def _save_membership_sync(
        self,
        membership: UniverseMembership,
    ) -> UniverseMembership:
        serialized, content_sha256 = _serialized(membership.to_document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lifecycle = self._require_security(
                connection,
                membership.exchange,
                membership.symbol,
            )
            if membership.member_from < lifecycle.listed_at:
                raise PointInTimeIntegrityError(
                    "Universe membership cannot begin before listing."
                )
            if (
                lifecycle.delisted_at is not None
                and (
                    membership.member_until is None
                    or membership.member_until > lifecycle.delisted_at
                )
            ):
                raise PointInTimeIntegrityError(
                    "Universe membership cannot extend beyond delisting."
                )
            row = connection.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM universe_memberships "
                "WHERE membership_id = ?",
                (membership.membership_id,),
            ).fetchone()
            if row is not None:
                existing = self._membership_from_row(row)
                if existing != membership:
                    raise PointInTimeConflictError(
                        "Universe membership ID conflicts with stored data."
                    )
                return existing
            existing_rows = connection.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM universe_memberships "
                "WHERE universe_id = ? AND exchange = ? AND symbol = ? "
                "ORDER BY member_from ASC, membership_id ASC",
                (
                    membership.universe_id,
                    membership.exchange,
                    membership.symbol,
                ),
            ).fetchall()
            for existing_row in existing_rows:
                existing = self._membership_from_row(existing_row)
                if _intervals_overlap(
                    membership.member_from,
                    membership.member_until,
                    existing.member_from,
                    existing.member_until,
                ):
                    raise PointInTimeOverlapError(
                        "Universe membership intervals cannot overlap."
                    )
            connection.execute(
                """
                INSERT INTO universe_memberships(
                    membership_id,
                    universe_id,
                    exchange,
                    symbol,
                    member_from,
                    member_until,
                    available_at,
                    membership_json,
                    membership_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    membership.membership_id,
                    membership.universe_id,
                    membership.exchange,
                    membership.symbol,
                    _iso(membership.member_from),
                    _optional_iso(membership.member_until),
                    _iso(membership.available_at),
                    serialized,
                    content_sha256,
                ),
            )
        return membership

    def _save_availability_sync(
        self,
        availability: DataAvailabilityRecord,
    ) -> DataAvailabilityRecord:
        serialized, content_sha256 = _serialized(availability.to_document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lifecycle = self._require_security(
                connection,
                availability.exchange,
                availability.symbol,
            )
            if not lifecycle.is_listed_at(availability.effective_at):
                raise PointInTimeIntegrityError(
                    "Data effective timestamp must fall within the listing lifecycle."
                )
            row = connection.execute(
                f"SELECT {_AVAILABILITY_COLUMNS} FROM data_availability "
                "WHERE data_id = ?",
                (availability.data_id,),
            ).fetchone()
            if row is not None:
                existing = self._availability_from_row(row)
                if existing != availability:
                    raise PointInTimeConflictError(
                        "Data availability is immutable and already differs."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO data_availability(
                    data_id,
                    availability_id,
                    data_kind,
                    exchange,
                    symbol,
                    effective_at,
                    available_at,
                    availability_json,
                    availability_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    availability.data_id,
                    availability.availability_id,
                    availability.data_kind.value,
                    availability.exchange,
                    availability.symbol,
                    _iso(availability.effective_at),
                    _iso(availability.available_at),
                    serialized,
                    content_sha256,
                ),
            )
        return availability

    def _save_delisting_sync(self, delisting: DelistingRecord) -> DelistingRecord:
        serialized, content_sha256 = _serialized(delisting.to_document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lifecycle = self._require_security(
                connection,
                delisting.exchange,
                delisting.symbol,
            )
            if lifecycle.delisted_at is None or lifecycle.tradable_until is None:
                raise PointInTimeIntegrityError(
                    "Delisting records require a lifecycle with delisting boundaries."
                )
            if lifecycle.delisted_at != delisting.delisted_at:
                raise PointInTimeIntegrityError(
                    "Delisting timestamp must match the security lifecycle."
                )
            if lifecycle.tradable_until != delisting.last_tradable_at:
                raise PointInTimeIntegrityError(
                    "Last-tradable timestamp must match the security lifecycle."
                )
            row = connection.execute(
                f"SELECT {_DELISTING_COLUMNS} FROM delistings "
                "WHERE exchange = ? AND symbol = ?",
                (delisting.exchange, delisting.symbol),
            ).fetchone()
            if row is not None:
                existing = self._delisting_from_row(row)
                if existing != delisting:
                    raise PointInTimeConflictError(
                        "Delisting record is immutable and already differs."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO delistings(
                    exchange,
                    symbol,
                    delisting_id,
                    delisted_at,
                    delisting_json,
                    delisting_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delisting.exchange,
                    delisting.symbol,
                    delisting.delisting_id,
                    _iso(delisting.delisted_at),
                    serialized,
                    content_sha256,
                ),
            )
        return delisting

    def _get_security_sync(self, exchange: str, symbol: str) -> SecurityLifecycle:
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = _normalized_code(symbol)
        with self._connect() as connection:
            return self._require_security(
                connection,
                normalized_exchange,
                normalized_symbol,
            )

    def _get_availability_sync(self, data_id: str) -> DataAvailabilityRecord:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_AVAILABILITY_COLUMNS} FROM data_availability "
                "WHERE data_id = ?",
                (data_id,),
            ).fetchone()
            if row is None:
                raise PointInTimeNotFoundError(
                    "Data availability record does not exist."
                )
            return self._availability_from_row(row)

    def _get_delisting_sync(self, exchange: str, symbol: str) -> DelistingRecord:
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = _normalized_code(symbol)
        with self._connect() as connection:
            return self._require_delisting(
                connection,
                normalized_exchange,
                normalized_symbol,
            )

    def _build_snapshot_sync(
        self,
        universe_id: str,
        as_of: datetime,
        policy: PointInTimePolicy,
    ) -> PointInTimeSnapshot:
        _require_aware(as_of, "Snapshot as-of timestamp")
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM universe_memberships "
                "WHERE universe_id = ? AND member_from <= ? "
                "AND (member_until IS NULL OR member_until > ?) "
                "ORDER BY exchange ASC, symbol ASC, membership_id ASC",
                (universe_id, _iso(as_of), _iso(as_of)),
            ).fetchall()
            if not rows:
                raise PointInTimeNotFoundError(
                    "Point-in-time universe has no effective membership records."
                )
            members: list[PointInTimeMember] = []
            for row in rows:
                membership = self._membership_from_row(row)
                if not membership.is_known_at(as_of):
                    raise PointInTimeEligibilityError(
                        "Universe membership was not available at the as-of time."
                    )
                lifecycle = self._require_security(
                    connection,
                    membership.exchange,
                    membership.symbol,
                )
                if not lifecycle.is_tradable_at(as_of):
                    raise PointInTimeIntegrityError(
                        "Effective universe membership references a "
                        "non-tradable security."
                    )
                self._require_delisting_if_needed(connection, lifecycle, policy)
                members.append(
                    PointInTimeMember(
                        exchange=membership.exchange,
                        symbol=membership.symbol,
                        lifecycle_id=lifecycle.lifecycle_id,
                        membership_id=membership.membership_id,
                    )
                )
        return PointInTimeSnapshot(
            universe_id=universe_id,
            as_of=as_of,
            policy=policy,
            members=tuple(
                sorted(members, key=lambda item: (item.exchange, item.symbol))
            ),
        )

    def _build_backtest_context_sync(
        self,
        universe_id: str,
        exchange: str,
        symbol: str,
        start: datetime,
        end: datetime,
        policy: PointInTimePolicy,
    ) -> PointInTimeBacktestContext:
        _require_aware(start, "Backtest context start")
        _require_aware(end, "Backtest context end")
        if start >= end:
            raise PointInTimeIntegrityError(
                "Backtest context requires start before end."
            )
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = _normalized_code(symbol)
        with self._connect() as connection:
            lifecycle = self._require_security(
                connection,
                normalized_exchange,
                normalized_symbol,
            )
            if not lifecycle.is_tradable_at(start):
                raise PointInTimeEligibilityError(
                    "Security is not tradable at the requested backtest start."
                )
            if (
                lifecycle.tradable_until is not None
                and end > lifecycle.tradable_until
            ):
                raise PointInTimeEligibilityError(
                    "Backtest period extends beyond the tradable lifecycle."
                )
            membership_rows = connection.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM universe_memberships "
                "WHERE universe_id = ? AND exchange = ? AND symbol = ? "
                "AND member_from < ? "
                "AND (member_until IS NULL OR member_until > ?) "
                "ORDER BY member_from ASC, membership_id ASC",
                (
                    universe_id,
                    normalized_exchange,
                    normalized_symbol,
                    _iso(end),
                    _iso(start),
                ),
            ).fetchall()
            memberships = tuple(
                self._membership_from_row(row) for row in membership_rows
            )
            known_intervals = tuple(
                (
                    max(membership.member_from, membership.available_at),
                    membership.member_until,
                )
                for membership in memberships
            )
            if not _intervals_cover(start, end, known_intervals):
                raise PointInTimeEligibilityError(
                    "Known universe membership does not cover the backtest period."
                )
            availability_rows = connection.execute(
                f"SELECT {_AVAILABILITY_COLUMNS} FROM data_availability "
                "WHERE exchange = ? AND symbol = ? AND data_kind = ? "
                "AND effective_at >= ? AND effective_at < ? "
                "ORDER BY effective_at ASC, data_id ASC",
                (
                    normalized_exchange,
                    normalized_symbol,
                    PointInTimeDataKind.CANDLE.value,
                    _iso(start),
                    _iso(end),
                ),
            ).fetchall()
            availabilities = tuple(
                self._availability_from_row(row) for row in availability_rows
            )
            if not availabilities:
                raise PointInTimeEligibilityError(
                    "Backtest period has no registered data-availability records."
                )
            delisting = self._require_delisting_if_needed(
                connection,
                lifecycle,
                policy,
            )
        return PointInTimeBacktestContext(
            universe_id=universe_id,
            exchange=normalized_exchange,
            symbol=normalized_symbol,
            start=start,
            end=end,
            policy=policy,
            lifecycle_id=lifecycle.lifecycle_id,
            membership_ids=tuple(
                sorted(membership.membership_id for membership in memberships)
            ),
            availability_ids=tuple(
                sorted(availability.availability_id for availability in availabilities)
            ),
            delisting_id=None if delisting is None else delisting.delisting_id,
        )

    def _validate_access_sync(
        self,
        universe_id: str,
        exchange: str,
        symbol: str,
        data_id: str,
        event_at: datetime,
        decision_at: datetime,
        policy: PointInTimePolicy,
    ) -> PointInTimeAccessGrant:
        _require_aware(event_at, "Data event timestamp")
        _require_aware(decision_at, "Decision timestamp")
        if decision_at < event_at:
            raise PointInTimeEligibilityError(
                "Decision time cannot precede the data event."
            )
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = _normalized_code(symbol)
        with self._connect() as connection:
            lifecycle = self._require_security(
                connection,
                normalized_exchange,
                normalized_symbol,
            )
            if not lifecycle.is_tradable_at(event_at):
                raise PointInTimeEligibilityError(
                    "Security was not tradable at the data event time."
                )
            membership_rows = connection.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM universe_memberships "
                "WHERE universe_id = ? AND exchange = ? AND symbol = ? "
                "AND member_from <= ? "
                "AND (member_until IS NULL OR member_until > ?) "
                "ORDER BY membership_id ASC",
                (
                    universe_id,
                    normalized_exchange,
                    normalized_symbol,
                    _iso(event_at),
                    _iso(event_at),
                ),
            ).fetchall()
            if len(membership_rows) != 1:
                raise PointInTimeEligibilityError(
                    "Exactly one effective universe membership is required."
                )
            membership = self._membership_from_row(membership_rows[0])
            if not membership.is_known_at(decision_at):
                raise PointInTimeEligibilityError(
                    "Universe membership was not known at the decision time."
                )
            availability_row = connection.execute(
                f"SELECT {_AVAILABILITY_COLUMNS} FROM data_availability "
                "WHERE data_id = ?",
                (data_id,),
            ).fetchone()
            if availability_row is None:
                raise PointInTimeEligibilityError(
                    "Data has no point-in-time availability record."
                )
            availability = self._availability_from_row(availability_row)
            if availability.data_kind is not PointInTimeDataKind.CANDLE:
                raise PointInTimeEligibilityError(
                    "Backtest replay requires candle availability metadata."
                )
            if (
                availability.exchange != normalized_exchange
                or availability.symbol != normalized_symbol
                or availability.effective_at.astimezone(UTC)
                != event_at.astimezone(UTC)
            ):
                raise PointInTimeIntegrityError(
                    "Data availability metadata does not match the replay event."
                )
            if not availability.is_available_at(decision_at):
                raise PointInTimeEligibilityError(
                    "Data was not available at the decision time."
                )
            self._require_delisting_if_needed(connection, lifecycle, policy)
        return PointInTimeAccessGrant(
            data_id=data_id,
            exchange=normalized_exchange,
            symbol=normalized_symbol,
            event_at=event_at,
            decision_at=decision_at,
            lifecycle_id=lifecycle.lifecycle_id,
            membership_id=membership.membership_id,
            availability_id=availability.availability_id,
        )

    def _require_security(
        self,
        connection: sqlite3.Connection,
        exchange: str,
        symbol: str,
    ) -> SecurityLifecycle:
        row = connection.execute(
            f"SELECT {_LIFECYCLE_COLUMNS} FROM security_lifecycles "
            "WHERE exchange = ? AND symbol = ?",
            (exchange, symbol),
        ).fetchone()
        if row is None:
            raise PointInTimeNotFoundError("Security lifecycle does not exist.")
        return self._lifecycle_from_row(row)

    def _require_delisting(
        self,
        connection: sqlite3.Connection,
        exchange: str,
        symbol: str,
    ) -> DelistingRecord:
        row = connection.execute(
            f"SELECT {_DELISTING_COLUMNS} FROM delistings "
            "WHERE exchange = ? AND symbol = ?",
            (exchange, symbol),
        ).fetchone()
        if row is None:
            raise PointInTimeNotFoundError("Delisting record does not exist.")
        return self._delisting_from_row(row)

    def _require_delisting_if_needed(
        self,
        connection: sqlite3.Connection,
        lifecycle: SecurityLifecycle,
        _policy: PointInTimePolicy,
    ) -> DelistingRecord | None:
        if lifecycle.delisted_at is None:
            return None
        row = connection.execute(
            f"SELECT {_DELISTING_COLUMNS} FROM delistings "
            "WHERE exchange = ? AND symbol = ?",
            (lifecycle.exchange, lifecycle.symbol),
        ).fetchone()
        if row is None:
            raise PointInTimeEligibilityError(
                "Delisted security is missing an explicit delisting record."
            )
        delisting = self._delisting_from_row(row)
        if delisting.delisted_at != lifecycle.delisted_at:
            raise PointInTimeIntegrityError(
                "Delisting metadata conflicts with the security lifecycle."
            )
        return delisting

    def _lifecycle_from_row(self, row: Sequence[object]) -> SecurityLifecycle:
        exchange, symbol, lifecycle_id, raw_json, raw_sha256 = row
        document = self._verified_document(raw_json, raw_sha256)
        lifecycle = security_lifecycle_from_document(document)
        if (
            lifecycle.exchange != exchange
            or lifecycle.symbol != symbol
            or lifecycle.lifecycle_id != lifecycle_id
        ):
            raise PointInTimeIntegrityError(
                "Security lifecycle metadata does not match stored JSON."
            )
        return lifecycle

    def _membership_from_row(self, row: Sequence[object]) -> UniverseMembership:
        (
            membership_id,
            universe_id,
            exchange,
            symbol,
            member_from,
            member_until,
            available_at,
            raw_json,
            raw_sha256,
        ) = row
        document = self._verified_document(raw_json, raw_sha256)
        membership = universe_membership_from_document(document)
        if (
            membership.membership_id != membership_id
            or membership.universe_id != universe_id
            or membership.exchange != exchange
            or membership.symbol != symbol
            or _iso(membership.member_from) != member_from
            or _optional_iso(membership.member_until) != member_until
            or _iso(membership.available_at) != available_at
        ):
            raise PointInTimeIntegrityError(
                "Universe membership metadata does not match stored JSON."
            )
        return membership

    def _availability_from_row(
        self,
        row: Sequence[object],
    ) -> DataAvailabilityRecord:
        (
            data_id,
            availability_id,
            data_kind,
            exchange,
            symbol,
            effective_at,
            available_at,
            raw_json,
            raw_sha256,
        ) = row
        document = self._verified_document(raw_json, raw_sha256)
        availability = data_availability_from_document(document)
        if (
            availability.data_id != data_id
            or availability.availability_id != availability_id
            or availability.data_kind.value != data_kind
            or availability.exchange != exchange
            or availability.symbol != symbol
            or _iso(availability.effective_at) != effective_at
            or _iso(availability.available_at) != available_at
        ):
            raise PointInTimeIntegrityError(
                "Data availability metadata does not match stored JSON."
            )
        return availability

    def _delisting_from_row(self, row: Sequence[object]) -> DelistingRecord:
        exchange, symbol, delisting_id, delisted_at, raw_json, raw_sha256 = row
        document = self._verified_document(raw_json, raw_sha256)
        delisting = delisting_record_from_document(document)
        if (
            delisting.exchange != exchange
            or delisting.symbol != symbol
            or delisting.delisting_id != delisting_id
            or _iso(delisting.delisted_at) != delisted_at
        ):
            raise PointInTimeIntegrityError(
                "Delisting metadata does not match stored JSON."
            )
        return delisting

    def _verified_document(
        self,
        raw_json: object,
        raw_sha256: object,
    ) -> Mapping[str, object]:
        if not isinstance(raw_json, str) or not isinstance(raw_sha256, str):
            raise PointInTimeIntegrityError(
                "Stored point-in-time JSON metadata is invalid."
            )
        encoded = raw_json.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != raw_sha256:
            raise PointInTimeIntegrityError(
                "Stored point-in-time JSON failed SHA-256 verification."
            )
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as error:
            raise PointInTimeIntegrityError(
                "Stored point-in-time JSON is invalid."
            ) from error
        if not isinstance(parsed, dict):
            raise PointInTimeIntegrityError(
                "Stored point-in-time JSON must contain an object."
            )
        return cast(dict[str, object], parsed)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS security_lifecycles(
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    lifecycle_id TEXT NOT NULL UNIQUE,
                    listed_at TEXT NOT NULL,
                    delisted_at TEXT,
                    tradable_from TEXT NOT NULL,
                    tradable_until TEXT,
                    lifecycle_json TEXT NOT NULL,
                    lifecycle_sha256 TEXT NOT NULL,
                    PRIMARY KEY(exchange, symbol)
                );

                CREATE TABLE IF NOT EXISTS universe_memberships(
                    membership_id TEXT PRIMARY KEY,
                    universe_id TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    member_from TEXT NOT NULL,
                    member_until TEXT,
                    available_at TEXT NOT NULL,
                    membership_json TEXT NOT NULL,
                    membership_sha256 TEXT NOT NULL,
                    FOREIGN KEY(exchange, symbol)
                        REFERENCES security_lifecycles(exchange, symbol)
                );

                CREATE INDEX IF NOT EXISTS idx_memberships_lookup
                ON universe_memberships(
                    universe_id,
                    exchange,
                    symbol,
                    member_from,
                    member_until
                );

                CREATE TABLE IF NOT EXISTS data_availability(
                    data_id TEXT PRIMARY KEY,
                    availability_id TEXT NOT NULL UNIQUE,
                    data_kind TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    availability_json TEXT NOT NULL,
                    availability_sha256 TEXT NOT NULL,
                    FOREIGN KEY(exchange, symbol)
                        REFERENCES security_lifecycles(exchange, symbol)
                );

                CREATE INDEX IF NOT EXISTS idx_availability_lookup
                ON data_availability(exchange, symbol, effective_at, data_id);

                CREATE TABLE IF NOT EXISTS delistings(
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    delisting_id TEXT NOT NULL UNIQUE,
                    delisted_at TEXT NOT NULL,
                    delisting_json TEXT NOT NULL,
                    delisting_sha256 TEXT NOT NULL,
                    PRIMARY KEY(exchange, symbol),
                    FOREIGN KEY(exchange, symbol)
                        REFERENCES security_lifecycles(exchange, symbol)
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


class PointInTimeValidatedCandleReader:
    """Fail-closed wrapper that validates every replay candle before exposure."""

    def __init__(
        self,
        reader: NormalizedMarketDataReader,
        store: SQLitePointInTimeStore,
        context: PointInTimeBacktestContext,
    ) -> None:
        self._reader = reader
        self._store = store
        self._context = context

    @property
    def context_digest(self) -> str:
        return self._context.context_digest

    async def query_candles(
        self,
        *,
        symbols: Sequence[str] | None = None,
        interval: CandleInterval | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        statuses: Sequence[QualityStatus] | None = None,
        after: NormalizedCandleCursor | None = None,
        limit: int = 1_000,
    ) -> tuple[NormalizedCandleRecord, ...]:
        records = await self._reader.query_candles(
            symbols=symbols,
            interval=interval,
            start=start,
            end=end,
            statuses=statuses,
            after=after,
            limit=limit,
        )
        for record in records:
            candle = record.candle
            if (
                candle.symbol.upper() != self._context.symbol
                or candle.timestamp < self._context.start
                or candle.timestamp >= self._context.end
            ):
                raise PointInTimeEligibilityError(
                    "Replay candle falls outside the validated point-in-time context."
                )
            grant = await self._store.validate_access(
                universe_id=self._context.universe_id,
                exchange=self._context.exchange,
                symbol=self._context.symbol,
                data_id=record.item_id,
                event_at=candle.timestamp,
                decision_at=candle.timestamp,
                policy=self._context.policy,
            )
            if (
                grant.lifecycle_id != self._context.lifecycle_id
                or grant.membership_id not in self._context.membership_ids
                or grant.availability_id not in self._context.availability_ids
            ):
                raise PointInTimeIntegrityError(
                    "Replay metadata is not pinned by the validated context digest."
                )
        return records


def _serialized(document: Mapping[str, object]) -> tuple[str, str]:
    encoded = canonical_json_bytes(document)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _normalized_code(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PointInTimeIntegrityError("Exchange and symbol cannot be empty.")
    return value.strip().upper()


def _require_aware(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PointInTimeIntegrityError(
            f"{field_name} must include timezone information."
        )


def _intervals_overlap(
    first_start: datetime,
    first_end: datetime | None,
    second_start: datetime,
    second_end: datetime | None,
) -> bool:
    first_limit = datetime.max.replace(tzinfo=UTC) if first_end is None else first_end
    second_limit = (
        datetime.max.replace(tzinfo=UTC) if second_end is None else second_end
    )
    return first_start < second_limit and second_start < first_limit


def _intervals_cover(
    start: datetime,
    end: datetime,
    intervals: Sequence[tuple[datetime, datetime | None]],
) -> bool:
    cursor = start.astimezone(UTC)
    target = end.astimezone(UTC)
    for interval_start, interval_end in sorted(intervals, key=lambda item: item[0]):
        current_start = max(interval_start.astimezone(UTC), cursor)
        current_end = (
            target
            if interval_end is None
            else min(interval_end.astimezone(UTC), target)
        )
        if current_end <= cursor:
            continue
        if current_start > cursor:
            return False
        cursor = current_end
        if cursor >= target:
            return True
    return cursor >= target


class PointInTimeValidatedMultiSymbolCandleReader:
    """Fail-closed point-in-time validation for one symbol-change path."""

    def __init__(
        self,
        reader: NormalizedMarketDataReader,
        store: SQLitePointInTimeStore,
        contexts: tuple[PointInTimeBacktestContext, ...],
    ) -> None:
        if not contexts:
            raise PointInTimeIntegrityError(
                "Multi-symbol validation requires at least one context."
            )
        by_symbol: dict[str, PointInTimeBacktestContext] = {}
        first = contexts[0]
        for context in contexts:
            if context.symbol in by_symbol:
                raise PointInTimeIntegrityError(
                    "Multi-symbol point-in-time contexts cannot repeat symbols."
                )
            if (
                context.universe_id != first.universe_id
                or context.exchange != first.exchange
                or context.policy != first.policy
            ):
                raise PointInTimeIntegrityError(
                    "Multi-symbol contexts must share universe, venue, and policy."
                )
            by_symbol[context.symbol] = context
        self._reader = reader
        self._store = store
        self._contexts = tuple(sorted(contexts, key=lambda item: item.symbol))
        self._by_symbol = by_symbol

    @property
    def context_digest(self) -> str:
        documents = [context.to_document() for context in self._contexts]
        return hashlib.sha256(canonical_json_bytes(documents)).hexdigest()

    async def query_candles(
        self,
        *,
        symbols: Sequence[str] | None = None,
        interval: CandleInterval | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        statuses: Sequence[QualityStatus] | None = None,
        after: NormalizedCandleCursor | None = None,
        limit: int = 1_000,
    ) -> tuple[NormalizedCandleRecord, ...]:
        records = await self._reader.query_candles(
            symbols=symbols,
            interval=interval,
            start=start,
            end=end,
            statuses=statuses,
            after=after,
            limit=limit,
        )
        for record in records:
            candle = record.candle
            context = self._by_symbol.get(candle.symbol.upper())
            if context is None:
                raise PointInTimeEligibilityError(
                    "Replay candle symbol is not pinned by the validated contexts."
                )
            if not context.start <= candle.timestamp < context.end:
                raise PointInTimeEligibilityError(
                    "Replay candle falls outside its point-in-time context."
                )
            grant = await self._store.validate_access(
                universe_id=context.universe_id,
                exchange=context.exchange,
                symbol=context.symbol,
                data_id=record.item_id,
                event_at=candle.timestamp,
                decision_at=candle.timestamp,
                policy=context.policy,
            )
            if (
                grant.lifecycle_id != context.lifecycle_id
                or grant.membership_id not in context.membership_ids
                or grant.availability_id not in context.availability_ids
            ):
                raise PointInTimeIntegrityError(
                    "Replay metadata is not pinned by the validated context digest."
                )
        return records


__all__ = [
    "PointInTimeValidatedCandleReader",
    "PointInTimeValidatedMultiSymbolCandleReader",
    "SQLitePointInTimeStore",
]
