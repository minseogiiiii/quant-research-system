from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from world_quant_system.paper_execution.models import (
    BrokerCertificationReport,
    BrokerOrderSnapshot,
    KillSwitchMode,
    KillSwitchState,
    OrderEventType,
    PaperExecutionIntegrityError,
    PaperOrderEvent,
    PaperOrderIntent,
    PaperOrderRecord,
    PaperOrderStatus,
    ReconciliationReport,
    format_utc,
    parse_order_intent,
    parse_utc_datetime,
    validate_order_transition,
)

_SCHEMA_VERSION = 1
_GENESIS_HASH = "0" * 64


class SQLitePaperExecutionStore:
    """Crash-safe append-only paper-order and reconciliation store."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def create_intent(
        self,
        intent: PaperOrderIntent,
        *,
        created_at: datetime,
    ) -> tuple[PaperOrderRecord, bool]:
        with self._transaction() as connection:
            existing = self._get_order(connection, intent.client_order_id)
            if existing is not None:
                if existing.intent.intent_id != intent.intent_id:
                    raise PaperExecutionIntegrityError(
                        "Client order ID collision with a different intent."
                    )
                return existing, False
            event = self._new_event(
                connection,
                client_order_id=intent.client_order_id,
                timestamp=created_at,
                event_type=OrderEventType.INTENT_CREATED,
                previous_status=None,
                new_status=PaperOrderStatus.CREATED,
                reason="deterministic paper order intent persisted",
                broker_order_id=None,
                previous_hash=_GENESIS_HASH,
            )
            connection.execute(
                """
                INSERT INTO paper_orders (
                    client_order_id,
                    intent_json,
                    intent_id,
                    status,
                    broker_order_id,
                    filled_quantity,
                    average_fill_price,
                    rejection_reason,
                    created_at,
                    updated_at,
                    version,
                    last_event_hash
                ) VALUES (?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?, 0, ?)
                """,
                (
                    intent.client_order_id,
                    _json_text(intent.to_document()),
                    intent.intent_id,
                    PaperOrderStatus.CREATED.value,
                    format_utc(created_at),
                    format_utc(created_at),
                    event.event_hash,
                ),
            )
            self._insert_event(connection, event)
            record = self._get_order(connection, intent.client_order_id)
            if record is None:
                raise PaperExecutionIntegrityError(
                    "Persisted paper order could not be reloaded."
                )
            return record, True

    def get_order(self, client_order_id: str) -> PaperOrderRecord | None:
        with self._connect() as connection:
            return self._get_order(connection, client_order_id)

    def list_orders(self) -> tuple[PaperOrderRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_orders ORDER BY client_order_id"
            ).fetchall()
            return tuple(self._record_from_row(row) for row in rows)

    def list_events(
        self,
        client_order_id: str | None = None,
    ) -> tuple[PaperOrderEvent, ...]:
        with self._connect() as connection:
            if client_order_id is None:
                rows = connection.execute(
                    "SELECT event_json FROM order_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT event_json
                    FROM order_events
                    WHERE client_order_id = ?
                    ORDER BY sequence
                    """,
                    (client_order_id,),
                ).fetchall()
        return tuple(
            _event_from_document(json.loads(row["event_json"]))
            for row in rows
        )

    def transition(
        self,
        client_order_id: str,
        *,
        target_status: PaperOrderStatus,
        event_type: OrderEventType,
        reason: str,
        updated_at: datetime,
        broker_order_id: str | None = None,
        filled_quantity: int | None = None,
        average_fill_price: Decimal | None = None,
        rejection_reason: str | None = None,
    ) -> PaperOrderRecord:
        with self._transaction() as connection:
            current = self._get_order(connection, client_order_id)
            if current is None:
                raise PaperExecutionIntegrityError(
                    f"Unknown client order ID: {client_order_id}."
                )
            validate_order_transition(current.status, target_status)
            next_broker_id = broker_order_id or current.broker_order_id
            next_filled = (
                current.filled_quantity
                if filled_quantity is None
                else filled_quantity
            )
            next_average = (
                current.average_fill_price
                if average_fill_price is None
                else average_fill_price
            )
            next_rejection = (
                current.rejection_reason
                if rejection_reason is None
                else rejection_reason
            )
            event = self._new_event(
                connection,
                client_order_id=client_order_id,
                timestamp=updated_at,
                event_type=event_type,
                previous_status=current.status,
                new_status=target_status,
                reason=reason,
                broker_order_id=next_broker_id,
                previous_hash=current.last_event_hash,
            )
            connection.execute(
                """
                UPDATE paper_orders
                SET status = ?,
                    broker_order_id = ?,
                    filled_quantity = ?,
                    average_fill_price = ?,
                    rejection_reason = ?,
                    updated_at = ?,
                    version = version + 1,
                    last_event_hash = ?
                WHERE client_order_id = ? AND version = ?
                """,
                (
                    target_status.value,
                    next_broker_id,
                    next_filled,
                    _optional_decimal_text(next_average),
                    next_rejection,
                    format_utc(updated_at),
                    event.event_hash,
                    client_order_id,
                    current.version,
                ),
            )
            if connection.total_changes < 1:
                raise PaperExecutionIntegrityError(
                    "Paper order optimistic update failed."
                )
            self._insert_event(connection, event)
            result = self._get_order(connection, client_order_id)
            if result is None:
                raise PaperExecutionIntegrityError(
                    "Updated paper order could not be reloaded."
                )
            return result

    def apply_broker_order(
        self,
        snapshot: BrokerOrderSnapshot,
        *,
        event_type: OrderEventType,
        reason: str,
    ) -> PaperOrderRecord:
        current = self.get_order(snapshot.client_order_id)
        if current is None:
            raise PaperExecutionIntegrityError(
                "Broker order does not match a known internal intent."
            )
        if current.intent.symbol != snapshot.symbol:
            raise PaperExecutionIntegrityError(
                "Broker order symbol differs from the stored intent."
            )
        if current.intent.side != snapshot.side:
            raise PaperExecutionIntegrityError(
                "Broker order side differs from the stored intent."
            )
        if current.intent.quantity != snapshot.quantity:
            raise PaperExecutionIntegrityError(
                "Broker order quantity differs from the stored intent."
            )
        if current.intent.limit_price != snapshot.limit_price:
            raise PaperExecutionIntegrityError(
                "Broker limit price differs from the stored intent."
            )
        return self.transition(
            snapshot.client_order_id,
            target_status=snapshot.status,
            event_type=event_type,
            reason=reason,
            updated_at=snapshot.updated_at,
            broker_order_id=snapshot.broker_order_id,
            filled_quantity=snapshot.filled_quantity,
            average_fill_price=snapshot.average_fill_price,
        )

    def get_kill_switch(self) -> KillSwitchState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kill_switch WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise PaperExecutionIntegrityError("Kill-switch state is missing.")
        activated_at = row["activated_at"]
        return KillSwitchState(
            mode=KillSwitchMode(row["mode"]),
            reason=row["reason"],
            activated_at=(
                None
                if activated_at is None
                else parse_utc_datetime(activated_at, "Kill-switch time")
            ),
            version=int(row["version"]),
        )

    def activate_kill_switch(
        self,
        mode: KillSwitchMode,
        *,
        reason: str,
        activated_at: datetime,
    ) -> KillSwitchState:
        if mode == KillSwitchMode.NORMAL:
            raise PaperExecutionIntegrityError(
                "Kill-switch activation requires a halt mode."
            )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM kill_switch WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise PaperExecutionIntegrityError("Kill-switch state is missing.")
            current_mode = KillSwitchMode(row["mode"])
            target = _stronger_kill_switch(current_mode, mode)
            if target == current_mode:
                return self.get_kill_switch()
            connection.execute(
                """
                UPDATE kill_switch
                SET mode = ?, reason = ?, activated_at = ?, version = version + 1
                WHERE singleton_id = 1
                """,
                (
                    target.value,
                    reason,
                    format_utc(activated_at),
                ),
            )
        return self.get_kill_switch()

    def reset_kill_switch(
        self,
        *,
        reason: str,
        reset_at: datetime,
        expected_version: int,
    ) -> KillSwitchState:
        if not reason.strip():
            raise PaperExecutionIntegrityError(
                "Kill-switch reset requires a human review reason."
            )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT version FROM kill_switch WHERE singleton_id = 1"
            ).fetchone()
            if row is None or int(row["version"]) != expected_version:
                raise PaperExecutionIntegrityError(
                    "Kill-switch reset version does not match current state."
                )
            connection.execute(
                """
                UPDATE kill_switch
                SET mode = ?, reason = ?, activated_at = NULL,
                    version = version + 1, last_reset_at = ?, last_reset_reason = ?
                WHERE singleton_id = 1 AND version = ?
                """,
                (
                    KillSwitchMode.NORMAL.value,
                    "normal",
                    format_utc(reset_at),
                    reason,
                    expected_version,
                ),
            )
        return self.get_kill_switch()

    def record_reconciliation(self, report: ReconciliationReport) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO reconciliation_reports (
                    report_id, report_digest, report_json, compared_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.report_digest,
                    _json_text(report.to_document()),
                    format_utc(report.compared_at),
                ),
            )

    def record_certification(self, report: BrokerCertificationReport) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO certification_reports (
                    report_id, report_digest, report_json, evaluated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.report_digest,
                    _json_text(report.to_document()),
                    format_utc(report.evaluated_at),
                ),
            )

    def latest_reconciliation_document(self) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT report_json
                FROM reconciliation_reports
                ORDER BY compared_at DESC, report_id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        parsed = json.loads(row["report_json"])
        if not isinstance(parsed, dict):
            raise PaperExecutionIntegrityError(
                "Stored reconciliation document is invalid."
            )
        return parsed

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    client_order_id TEXT PRIMARY KEY,
                    intent_json TEXT NOT NULL,
                    intent_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    broker_order_id TEXT UNIQUE,
                    filled_quantity INTEGER NOT NULL,
                    average_fill_price TEXT,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    last_event_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    sequence INTEGER PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    FOREIGN KEY(client_order_id)
                        REFERENCES paper_orders(client_order_id)
                        DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TABLE IF NOT EXISTS kill_switch (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    activated_at TEXT,
                    version INTEGER NOT NULL,
                    last_reset_at TEXT,
                    last_reset_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS reconciliation_reports (
                    report_id TEXT PRIMARY KEY,
                    report_digest TEXT NOT NULL UNIQUE,
                    report_json TEXT NOT NULL,
                    compared_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS certification_reports (
                    report_id TEXT PRIMARY KEY,
                    report_digest TEXT NOT NULL UNIQUE,
                    report_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO metadata (key, value)
                VALUES ('schema_version', ?)
                """,
                (str(_SCHEMA_VERSION),),
            )
            version_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version_row is None or int(version_row["value"]) != _SCHEMA_VERSION:
                raise PaperExecutionIntegrityError(
                    "Paper execution store schema version is incompatible."
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO kill_switch (
                    singleton_id, mode, reason, activated_at, version
                ) VALUES (1, ?, 'normal', NULL, 0)
                """,
                (KillSwitchMode.NORMAL.value,),
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _get_order(
        self,
        connection: sqlite3.Connection,
        client_order_id: str,
    ) -> PaperOrderRecord | None:
        row = connection.execute(
            "SELECT * FROM paper_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return None if row is None else self._record_from_row(row)

    def _record_from_row(self, row: sqlite3.Row) -> PaperOrderRecord:
        intent_document = json.loads(row["intent_json"])
        intent = parse_order_intent(intent_document)
        average_text = row["average_fill_price"]
        return PaperOrderRecord(
            intent=intent,
            status=PaperOrderStatus(row["status"]),
            broker_order_id=row["broker_order_id"],
            filled_quantity=int(row["filled_quantity"]),
            average_fill_price=(
                None if average_text is None else Decimal(average_text)
            ),
            rejection_reason=row["rejection_reason"],
            created_at=parse_utc_datetime(row["created_at"], "Creation time"),
            updated_at=parse_utc_datetime(row["updated_at"], "Update time"),
            version=int(row["version"]),
            last_event_hash=row["last_event_hash"],
        )

    def _new_event(
        self,
        connection: sqlite3.Connection,
        *,
        client_order_id: str,
        timestamp: datetime,
        event_type: OrderEventType,
        previous_status: PaperOrderStatus | None,
        new_status: PaperOrderStatus,
        reason: str,
        broker_order_id: str | None,
        previous_hash: str,
    ) -> PaperOrderEvent:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM order_events"
        ).fetchone()
        if row is None:
            raise PaperExecutionIntegrityError(
                "Unable to allocate an order-event sequence."
            )
        return PaperOrderEvent(
            sequence=int(row["next_sequence"]),
            client_order_id=client_order_id,
            timestamp=timestamp.astimezone(UTC),
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            reason=reason,
            broker_order_id=broker_order_id,
            previous_hash=previous_hash,
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        event: PaperOrderEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_events (
                sequence, client_order_id, event_hash, event_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                event.sequence,
                event.client_order_id,
                event.event_hash,
                _json_text(event.to_document()),
            ),
        )


def _event_from_document(document: object) -> PaperOrderEvent:
    if not isinstance(document, dict):
        raise PaperExecutionIntegrityError("Stored order event is invalid.")
    previous = document.get("previous_status")
    event = PaperOrderEvent(
        sequence=int(document["sequence"]),
        client_order_id=str(document["client_order_id"]),
        timestamp=parse_utc_datetime(document["timestamp"], "Event time"),
        event_type=OrderEventType(str(document["event_type"])),
        previous_status=(
            None if previous is None else PaperOrderStatus(str(previous))
        ),
        new_status=PaperOrderStatus(str(document["new_status"])),
        reason=str(document["reason"]),
        broker_order_id=(
            None
            if document.get("broker_order_id") is None
            else str(document["broker_order_id"])
        ),
        previous_hash=str(document["previous_hash"]),
    )
    if document.get("event_hash") != event.event_hash:
        raise PaperExecutionIntegrityError("Stored order event hash mismatch.")
    return event


def _stronger_kill_switch(
    current: KillSwitchMode,
    requested: KillSwitchMode,
) -> KillSwitchMode:
    rank = {
        KillSwitchMode.NORMAL: 0,
        KillSwitchMode.SOFT_HALT: 1,
        KillSwitchMode.CANCEL_ONLY: 2,
        KillSwitchMode.HARD_HALT: 3,
    }
    return requested if rank[requested] > rank[current] else current


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
