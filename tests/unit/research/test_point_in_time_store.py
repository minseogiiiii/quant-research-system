import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.research import (
    DataAvailabilityRecord,
    DelistingReason,
    DelistingRecord,
    PointInTimeConflictError,
    PointInTimeDataKind,
    PointInTimeEligibilityError,
    PointInTimeIntegrityError,
    PointInTimeOverlapError,
    SecurityLifecycle,
    SQLitePointInTimeStore,
    UniverseMembership,
)

START = datetime(2020, 1, 1, tzinfo=UTC)
END = datetime(2020, 1, 10, tzinfo=UTC)


def active_lifecycle() -> SecurityLifecycle:
    return SecurityLifecycle(
        exchange="XKRX",
        symbol="005930",
        listed_at=datetime(1975, 6, 11, tzinfo=UTC),
        tradable_from=datetime(1975, 6, 11, tzinfo=UTC),
        source="test",
        source_digest="a" * 64,
    )


def active_membership(
    *,
    available_at: datetime = START,
    member_from: datetime = START,
    member_until: datetime | None = END,
) -> UniverseMembership:
    return UniverseMembership(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        member_from=member_from,
        member_until=member_until,
        available_at=available_at,
        source="test",
        source_digest="b" * 64,
    )


def availability(
    data_id: str,
    effective_at: datetime,
    *,
    available_at: datetime | None = None,
) -> DataAvailabilityRecord:
    return DataAvailabilityRecord(
        data_id=data_id,
        data_kind=PointInTimeDataKind.CANDLE,
        exchange="XKRX",
        symbol="005930",
        effective_at=effective_at,
        available_at=effective_at if available_at is None else available_at,
        source="test",
        source_digest="c" * 64,
    )


async def seed_active_store(root: Path) -> SQLitePointInTimeStore:
    store = SQLitePointInTimeStore(root)
    await store.save_security(active_lifecycle())
    await store.save_membership(active_membership())
    for offset in range(1, 5):
        await store.save_availability(
            availability(f"candle-{offset}", START + timedelta(days=offset))
        )
    return store


@pytest.mark.asyncio
async def test_immutable_writes_are_idempotent_and_conflicts_fail(
    tmp_path: Path,
) -> None:
    store = SQLitePointInTimeStore(tmp_path / "pit")
    candidate = active_lifecycle()

    results = await asyncio.gather(*(store.save_security(candidate) for _ in range(20)))

    assert all(result == candidate for result in results)
    with pytest.raises(PointInTimeConflictError):
        await store.save_security(
            SecurityLifecycle(
                exchange="XKRX",
                symbol="005930",
                listed_at=datetime(1975, 6, 11, tzinfo=UTC),
                tradable_from=datetime(1975, 6, 12, tzinfo=UTC),
                source="test",
                source_digest="a" * 64,
            )
        )


@pytest.mark.asyncio
async def test_membership_overlap_is_blocked(tmp_path: Path) -> None:
    store = SQLitePointInTimeStore(tmp_path / "pit")
    await store.save_security(active_lifecycle())
    await store.save_membership(active_membership())

    with pytest.raises(PointInTimeOverlapError):
        await store.save_membership(
            active_membership(
                member_from=START + timedelta(days=5),
                member_until=END + timedelta(days=5),
            )
        )


@pytest.mark.asyncio
async def test_snapshot_fails_when_effective_membership_was_not_yet_known(
    tmp_path: Path,
) -> None:
    store = SQLitePointInTimeStore(tmp_path / "pit")
    await store.save_security(active_lifecycle())
    await store.save_membership(
        active_membership(available_at=START + timedelta(days=3))
    )

    with pytest.raises(PointInTimeEligibilityError):
        await store.build_snapshot("KOSPI", START + timedelta(days=1))

    snapshot = await store.build_snapshot("KOSPI", START + timedelta(days=4))
    assert tuple(member.symbol for member in snapshot.members) == ("005930",)


@pytest.mark.asyncio
async def test_delisted_security_requires_explicit_delisting_record(
    tmp_path: Path,
) -> None:
    store = SQLitePointInTimeStore(tmp_path / "pit")
    lifecycle = SecurityLifecycle(
        exchange="XKRX",
        symbol="000001",
        listed_at=datetime(1990, 1, 1, tzinfo=UTC),
        tradable_from=datetime(1990, 1, 1, tzinfo=UTC),
        tradable_until=datetime(2021, 6, 30, tzinfo=UTC),
        delisted_at=datetime(2021, 7, 1, tzinfo=UTC),
        source="test",
        source_digest="d" * 64,
    )
    await store.save_security(lifecycle)
    await store.save_membership(
        UniverseMembership(
            universe_id="OLD-KOSPI",
            exchange="XKRX",
            symbol="000001",
            member_from=datetime(2020, 1, 1, tzinfo=UTC),
            member_until=datetime(2021, 6, 30, tzinfo=UTC),
            available_at=datetime(2020, 1, 1, tzinfo=UTC),
            source="test",
            source_digest="e" * 64,
        )
    )

    with pytest.raises(PointInTimeEligibilityError):
        await store.build_snapshot(
            "OLD-KOSPI",
            datetime(2021, 1, 1, tzinfo=UTC),
        )

    delisting = DelistingRecord(
        exchange="XKRX",
        symbol="000001",
        last_tradable_at=datetime(2021, 6, 30, tzinfo=UTC),
        delisted_at=datetime(2021, 7, 1, tzinfo=UTC),
        available_at=datetime(2021, 6, 1, tzinfo=UTC),
        reason=DelistingReason.REGULATORY,
        source="test",
        source_digest="f" * 64,
    )
    await store.save_delisting(delisting)
    snapshot = await store.build_snapshot(
        "OLD-KOSPI",
        datetime(2021, 1, 1, tzinfo=UTC),
    )
    assert snapshot.members[0].symbol == "000001"


@pytest.mark.asyncio
async def test_backtest_context_and_access_fail_closed_on_future_data(
    tmp_path: Path,
) -> None:
    store = await seed_active_store(tmp_path / "pit")
    context = await store.build_backtest_context(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        start=START,
        end=START + timedelta(days=5),
    )

    assert len(context.availability_ids) == 4
    grant = await store.validate_access(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        data_id="candle-1",
        event_at=START + timedelta(days=1),
        decision_at=START + timedelta(days=1),
    )
    assert grant.symbol == "005930"

    await store.save_availability(
        availability(
            "future-candle",
            START + timedelta(days=5),
            available_at=START + timedelta(days=6),
        )
    )
    with pytest.raises(PointInTimeEligibilityError):
        await store.validate_access(
            universe_id="KOSPI",
            exchange="XKRX",
            symbol="005930",
            data_id="future-candle",
            event_at=START + timedelta(days=5),
            decision_at=START + timedelta(days=5),
        )


@pytest.mark.asyncio
async def test_context_requires_continuous_known_membership_coverage(
    tmp_path: Path,
) -> None:
    store = SQLitePointInTimeStore(tmp_path / "pit")
    await store.save_security(active_lifecycle())
    await store.save_membership(
        active_membership(member_until=START + timedelta(days=2))
    )
    await store.save_membership(
        active_membership(
            member_from=START + timedelta(days=3),
            member_until=END,
        )
    )
    await store.save_availability(
        availability("candle-1", START + timedelta(days=1))
    )

    with pytest.raises(PointInTimeEligibilityError):
        await store.build_backtest_context(
            universe_id="KOSPI",
            exchange="XKRX",
            symbol="005930",
            start=START,
            end=START + timedelta(days=5),
        )


@pytest.mark.asyncio
async def test_catalog_tampering_is_detected(tmp_path: Path) -> None:
    store = await seed_active_store(tmp_path / "pit")
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE security_lifecycles SET lifecycle_json = '{}' "
            "WHERE exchange = 'XKRX' AND symbol = '005930'"
        )

    with pytest.raises(PointInTimeIntegrityError):
        await store.get_security("XKRX", "005930")
