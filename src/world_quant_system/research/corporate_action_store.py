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

from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.research.corporate_action_models import (
    CorporateActionBacktestContext,
    CorporateActionConflictError,
    CorporateActionEligibilityError,
    CorporateActionIntegrityError,
    CorporateActionNotFoundError,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    corporate_action_from_document,
)

_ACTION_COLUMNS = (
    "action_id, exchange, symbol, action_type, effective_at, available_at, "
    "action_json, action_sha256"
)


class SQLiteCorporateActionStore:
    """Immutable, deterministic corporate-action catalog backed by SQLite."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise CorporateActionIntegrityError(
                "Corporate-action storage root must be a directory."
            )
        self._database = self._root / "corporate_actions.sqlite3"
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database(self) -> Path:
        return self._database

    async def save_action(self, action: CorporateActionRecord) -> CorporateActionRecord:
        if not isinstance(action, CorporateActionRecord):
            raise CorporateActionIntegrityError(
                "Corporate-action storage requires a CorporateActionRecord."
            )
        return await asyncio.to_thread(self._save_action_sync, action)

    async def get_action(self, action_id: str) -> CorporateActionRecord:
        return await asyncio.to_thread(self._get_action_sync, action_id)

    async def list_actions(
        self,
        *,
        exchange: str,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[CorporateActionRecord, ...]:
        return await asyncio.to_thread(
            self._list_actions_sync,
            exchange,
            symbol,
            start,
            end,
        )

    async def build_backtest_context(
        self,
        *,
        exchange: str,
        initial_symbol: str,
        start: datetime,
        end: datetime,
        policy: CorporateActionPolicy | None = None,
        expected_delisted_at: datetime | None = None,
    ) -> CorporateActionBacktestContext:
        return await asyncio.to_thread(
            self._build_backtest_context_sync,
            exchange,
            initial_symbol,
            start,
            end,
            policy or CorporateActionPolicy(),
            expected_delisted_at,
        )

    async def load_context_actions(
        self,
        context: CorporateActionBacktestContext,
    ) -> tuple[CorporateActionRecord, ...]:
        if not isinstance(context, CorporateActionBacktestContext):
            raise CorporateActionIntegrityError(
                "Loading actions requires a CorporateActionBacktestContext."
            )
        return await asyncio.to_thread(self._load_context_actions_sync, context)

    def _save_action_sync(self, action: CorporateActionRecord) -> CorporateActionRecord:
        serialized, content_sha256 = _serialized(action.to_document())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_id = connection.execute(
                f"SELECT {_ACTION_COLUMNS} FROM corporate_actions WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = self._action_from_row(existing_by_id)
                if existing != action:
                    raise CorporateActionConflictError(
                        "Corporate-action ID already contains different immutable data."
                    )
                return existing
            existing_by_key = connection.execute(
                f"SELECT {_ACTION_COLUMNS} FROM corporate_actions "
                "WHERE exchange = ? AND symbol = ? AND action_type = ? "
                "AND effective_at = ?",
                (
                    action.exchange,
                    action.symbol,
                    action.action_type.value,
                    _iso(action.effective_at),
                ),
            ).fetchone()
            if existing_by_key is not None:
                existing = self._action_from_row(existing_by_key)
                if existing != action:
                    raise CorporateActionConflictError(
                        "Corporate-action natural key already contains different data."
                    )
                return existing
            connection.execute(
                """
                INSERT INTO corporate_actions(
                    action_id,
                    exchange,
                    symbol,
                    action_type,
                    effective_at,
                    available_at,
                    action_json,
                    action_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.exchange,
                    action.symbol,
                    action.action_type.value,
                    _iso(action.effective_at),
                    _iso(action.available_at),
                    serialized,
                    content_sha256,
                ),
            )
        return action

    def _get_action_sync(self, action_id: str) -> CorporateActionRecord:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_ACTION_COLUMNS} FROM corporate_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise CorporateActionNotFoundError(
                    f"Corporate action does not exist: {action_id}"
                )
            return self._action_from_row(row)

    def _list_actions_sync(
        self,
        exchange: str,
        symbol: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[CorporateActionRecord, ...]:
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = None if symbol is None else _normalized_code(symbol)
        _require_optional_aware(start, "Action-list start")
        _require_optional_aware(end, "Action-list end")
        if start is not None and end is not None and start >= end:
            raise CorporateActionIntegrityError(
                "Action-list start must precede its end."
            )
        clauses = ["exchange = ?"]
        parameters: list[object] = [normalized_exchange]
        if normalized_symbol is not None:
            clauses.append("symbol = ?")
            parameters.append(normalized_symbol)
        if start is not None:
            clauses.append("effective_at >= ?")
            parameters.append(_iso(start))
        if end is not None:
            clauses.append("effective_at < ?")
            parameters.append(_iso(end))
        sql = (
            f"SELECT {_ACTION_COLUMNS} FROM corporate_actions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY effective_at, action_type, symbol, action_id"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            return tuple(self._action_from_row(row) for row in rows)

    def _build_backtest_context_sync(
        self,
        exchange: str,
        initial_symbol: str,
        start: datetime,
        end: datetime,
        policy: CorporateActionPolicy,
        expected_delisted_at: datetime | None,
    ) -> CorporateActionBacktestContext:
        normalized_exchange = _normalized_code(exchange)
        normalized_symbol = _normalized_code(initial_symbol)
        _require_aware(start, "Corporate-action context start")
        _require_aware(end, "Corporate-action context end")
        _require_optional_aware(expected_delisted_at, "Expected delisting timestamp")
        if start >= end:
            raise CorporateActionIntegrityError(
                "Corporate-action context requires start before end."
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_ACTION_COLUMNS} FROM corporate_actions "
                "WHERE exchange = ? "
                "ORDER BY effective_at, action_type, symbol, action_id",
                (normalized_exchange,),
            ).fetchall()
            all_actions = tuple(self._action_from_row(row) for row in rows)

        selected: list[CorporateActionRecord] = []
        symbol_path = [normalized_symbol]
        current_symbol = normalized_symbol
        relevant_actions = sorted(
            (
                action
                for action in all_actions
                if any(start <= event.event_at < end for event in action.events)
            ),
            key=lambda action: min(event.sort_key() for event in action.events),
        )
        for action in relevant_actions:
            if action.symbol != current_symbol:
                if action.symbol in symbol_path:
                    raise CorporateActionIntegrityError(
                        "Corporate action targets a retired symbol in the "
                        "selected security path."
                    )
                continue
            if policy.require_known_before_event:
                first_relevant_event = min(
                    event.event_at
                    for event in action.events
                    if start <= event.event_at < end
                )
                if action.available_at > first_relevant_event:
                    raise CorporateActionEligibilityError(
                        "Corporate action was not known before its first "
                        "relevant event."
                    )
            selected.append(action)
            if action.action_type is CorporateActionType.SYMBOL_CHANGE:
                assert action.new_symbol is not None
                if action.new_symbol in symbol_path:
                    raise CorporateActionIntegrityError(
                        "Corporate-action symbol changes cannot form a cycle."
                    )
                current_symbol = action.new_symbol
                symbol_path.append(current_symbol)

        selected_delistings = tuple(
            action
            for action in selected
            if action.action_type is CorporateActionType.DELISTING
        )
        if len(selected_delistings) > 1:
            raise CorporateActionIntegrityError(
                "A single-security backtest cannot contain multiple delistings."
            )
        if expected_delisted_at is not None and start <= expected_delisted_at < end:
            if not selected_delistings:
                raise CorporateActionEligibilityError(
                    "Point-in-time lifecycle requires explicit delisting economics."
                )
            if selected_delistings[0].effective_at != expected_delisted_at:
                raise CorporateActionIntegrityError(
                    "Delisting economics timestamp does not match security lifecycle."
                )
        if selected_delistings and selected[-1] is not selected_delistings[0]:
            raise CorporateActionIntegrityError(
                "No corporate action may follow a delisting in one security path."
            )

        documents = [
            action.to_document()
            for action in sorted(selected, key=lambda item: item.action_id)
        ]
        dataset_digest = hashlib.sha256(canonical_json_bytes(documents)).hexdigest()
        return CorporateActionBacktestContext(
            exchange=normalized_exchange,
            initial_symbol=normalized_symbol,
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            policy=policy,
            action_ids=tuple(sorted(action.action_id for action in selected)),
            symbols=tuple(symbol_path),
            action_dataset_digest=dataset_digest,
        )

    def _load_context_actions_sync(
        self,
        context: CorporateActionBacktestContext,
    ) -> tuple[CorporateActionRecord, ...]:
        with self._connect() as connection:
            actions: list[CorporateActionRecord] = []
            for action_id in context.action_ids:
                row = connection.execute(
                    f"SELECT {_ACTION_COLUMNS} FROM corporate_actions "
                    "WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if row is None:
                    raise CorporateActionIntegrityError(
                        "Corporate-action context references missing metadata."
                    )
                actions.append(self._action_from_row(row))
        documents = [
            action.to_document()
            for action in sorted(actions, key=lambda item: item.action_id)
        ]
        digest = hashlib.sha256(canonical_json_bytes(documents)).hexdigest()
        if digest != context.action_dataset_digest:
            raise CorporateActionIntegrityError(
                "Corporate-action context dataset digest no longer matches storage."
            )
        rebuilt = self._build_backtest_context_sync(
            context.exchange,
            context.initial_symbol,
            context.start,
            context.end,
            context.policy,
            None,
        )
        if rebuilt.context_digest != context.context_digest:
            raise CorporateActionIntegrityError(
                "Corporate-action context is not reproducible from current storage."
            )
        return tuple(
            sorted(
                actions,
                key=lambda action: min(
                    event.sort_key() for event in action.events
                ),
            )
        )

    def _action_from_row(self, row: Sequence[object]) -> CorporateActionRecord:
        if len(row) != 8:
            raise CorporateActionIntegrityError(
                "Corporate-action database row has an invalid shape."
            )
        (
            action_id,
            exchange,
            symbol,
            action_type,
            effective_at,
            available_at,
            payload,
            digest,
        ) = row
        document = self._verified_document(payload, digest)
        action = corporate_action_from_document(document)
        if (
            action.action_id != action_id
            or action.exchange != exchange
            or action.symbol != symbol
            or action.action_type.value != action_type
            or _iso(action.effective_at) != effective_at
            or _iso(action.available_at) != available_at
        ):
            raise CorporateActionIntegrityError(
                "Corporate-action indexed metadata does not match its signed document."
            )
        return action

    def _verified_document(
        self,
        serialized: object,
        expected_sha256: object,
    ) -> dict[str, object]:
        if not isinstance(serialized, str) or not isinstance(expected_sha256, str):
            raise CorporateActionIntegrityError(
                "Stored corporate-action payload has an invalid type."
            )
        actual = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if actual != expected_sha256:
            raise CorporateActionIntegrityError(
                "Stored corporate-action payload failed SHA-256 verification."
            )
        try:
            parsed = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise CorporateActionIntegrityError(
                "Stored corporate-action payload is not valid JSON."
            ) from error
        if not isinstance(parsed, dict):
            raise CorporateActionIntegrityError(
                "Stored corporate-action payload must be a JSON object."
            )
        return cast(dict[str, object], parsed)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS corporate_actions(
                    action_id TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    action_sha256 TEXT NOT NULL,
                    UNIQUE(exchange, symbol, action_type, effective_at)
                );

                CREATE INDEX IF NOT EXISTS idx_corporate_actions_lookup
                ON corporate_actions(exchange, symbol, effective_at, action_type);
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


def _serialized(document: Mapping[str, object]) -> tuple[str, str]:
    encoded = canonical_json_bytes(document)
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _normalized_code(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionIntegrityError("Exchange and symbol cannot be empty.")
    return value.strip().upper()


def _require_aware(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CorporateActionIntegrityError(
            f"{field_name} must include timezone information."
        )


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


__all__ = ["SQLiteCorporateActionStore"]
