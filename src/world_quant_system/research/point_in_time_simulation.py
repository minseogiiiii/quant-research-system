from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_quant_system.research.point_in_time_models import (
    DataAvailabilityRecord,
    DelistingReason,
    DelistingRecord,
    PointInTimeDataKind,
    PointInTimeEligibilityError,
    PointInTimeOverlapError,
    SecurityLifecycle,
    UniverseMembership,
)
from world_quant_system.research.point_in_time_store import SQLitePointInTimeStore


@dataclass(frozen=True, slots=True)
class PointInTimeSimulationResult:
    snapshot_digest_match: bool
    future_data_blocked: bool
    overlap_blocked: bool
    delisted_security_retained: bool
    context_digest_match: bool
    member_count: int


async def run_point_in_time_simulation() -> PointInTimeSimulationResult:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    active = SecurityLifecycle(
        exchange="XKRX",
        symbol="005930",
        listed_at=datetime(1975, 6, 11, tzinfo=UTC),
        tradable_from=datetime(1975, 6, 11, tzinfo=UTC),
        source="simulation",
        source_digest="a" * 64,
    )
    delisted = SecurityLifecycle(
        exchange="XKRX",
        symbol="000001",
        listed_at=datetime(1990, 1, 1, tzinfo=UTC),
        tradable_from=datetime(1990, 1, 1, tzinfo=UTC),
        tradable_until=datetime(2021, 6, 30, tzinfo=UTC),
        delisted_at=datetime(2021, 7, 1, tzinfo=UTC),
        source="simulation",
        source_digest="b" * 64,
    )
    memberships = (
        UniverseMembership(
            universe_id="KOSPI-SIM",
            exchange="XKRX",
            symbol="005930",
            member_from=start,
            member_until=datetime(2023, 1, 1, tzinfo=UTC),
            available_at=start,
            source="simulation",
            source_digest="c" * 64,
        ),
        UniverseMembership(
            universe_id="KOSPI-SIM",
            exchange="XKRX",
            symbol="000001",
            member_from=start,
            member_until=datetime(2021, 6, 30, tzinfo=UTC),
            available_at=start,
            source="simulation",
            source_digest="d" * 64,
        ),
    )
    delisting = DelistingRecord(
        exchange="XKRX",
        symbol="000001",
        last_tradable_at=datetime(2021, 6, 30, tzinfo=UTC),
        delisted_at=datetime(2021, 7, 1, tzinfo=UTC),
        available_at=datetime(2021, 6, 1, tzinfo=UTC),
        reason=DelistingReason.REGULATORY,
        source="simulation",
        source_digest="e" * 64,
    )
    availability = (
        DataAvailabilityRecord(
            data_id="candle-2020-01-02",
            data_kind=PointInTimeDataKind.CANDLE,
            exchange="XKRX",
            symbol="005930",
            effective_at=start + timedelta(days=1),
            available_at=start + timedelta(days=1),
            source="simulation",
            source_digest="f" * 64,
        ),
        DataAvailabilityRecord(
            data_id="candle-2020-01-03",
            data_kind=PointInTimeDataKind.CANDLE,
            exchange="XKRX",
            symbol="005930",
            effective_at=start + timedelta(days=2),
            available_at=start + timedelta(days=3),
            source="simulation",
            source_digest="1" * 64,
        ),
    )

    async def populate(root: Path, *, reverse: bool) -> SQLitePointInTimeStore:
        store = SQLitePointInTimeStore(root)
        lifecycles = (active, delisted)
        ordered_lifecycles = tuple(reversed(lifecycles)) if reverse else lifecycles
        for lifecycle in ordered_lifecycles:
            await store.save_security(lifecycle)
        ordered_memberships = tuple(reversed(memberships)) if reverse else memberships
        for membership in ordered_memberships:
            await store.save_membership(membership)
        await store.save_delisting(delisting)
        ordered_availability = (
            tuple(reversed(availability)) if reverse else availability
        )
        for record in ordered_availability:
            await store.save_availability(record)
        return store

    with tempfile.TemporaryDirectory() as directory:
        first_store = await populate(Path(directory) / "first", reverse=False)
        second_store = await populate(Path(directory) / "second", reverse=True)
        as_of = datetime(2021, 1, 1, tzinfo=UTC)
        first_snapshot = await first_store.build_snapshot("KOSPI-SIM", as_of)
        second_snapshot = await second_store.build_snapshot("KOSPI-SIM", as_of)
        first_context = await first_store.build_backtest_context(
            universe_id="KOSPI-SIM",
            exchange="XKRX",
            symbol="005930",
            start=start,
            end=datetime(2023, 1, 1, tzinfo=UTC),
        )
        second_context = await second_store.build_backtest_context(
            universe_id="KOSPI-SIM",
            exchange="XKRX",
            symbol="005930",
            start=start,
            end=datetime(2023, 1, 1, tzinfo=UTC),
        )
        future_blocked = False
        try:
            await first_store.validate_access(
                universe_id="KOSPI-SIM",
                exchange="XKRX",
                symbol="005930",
                data_id="candle-2020-01-03",
                event_at=start + timedelta(days=2),
                decision_at=start + timedelta(days=2),
            )
        except PointInTimeEligibilityError:
            future_blocked = True
        overlap_blocked = False
        try:
            await first_store.save_membership(
                UniverseMembership(
                    universe_id="KOSPI-SIM",
                    exchange="XKRX",
                    symbol="005930",
                    member_from=datetime(2022, 1, 1, tzinfo=UTC),
                    member_until=datetime(2024, 1, 1, tzinfo=UTC),
                    available_at=datetime(2021, 12, 1, tzinfo=UTC),
                    source="simulation",
                    source_digest="2" * 64,
                )
            )
        except PointInTimeOverlapError:
            overlap_blocked = True
        retained = any(member.symbol == "000001" for member in first_snapshot.members)
        return PointInTimeSimulationResult(
            snapshot_digest_match=(
                first_snapshot.snapshot_digest == second_snapshot.snapshot_digest
            ),
            future_data_blocked=future_blocked,
            overlap_blocked=overlap_blocked,
            delisted_security_retained=retained,
            context_digest_match=(
                first_context.context_digest == second_context.context_digest
            ),
            member_count=len(first_snapshot.members),
        )


def main() -> None:
    result = asyncio.run(run_point_in_time_simulation())
    print(f"Point-in-time members: {result.member_count}")
    print(
        "Deterministic snapshot digest: "
        f"{'match' if result.snapshot_digest_match else 'mismatch'}"
    )
    print(
        "Deterministic context digest: "
        f"{'match' if result.context_digest_match else 'mismatch'}"
    )
    print(
        "Future-data access blocked: "
        f"{'yes' if result.future_data_blocked else 'no'}"
    )
    print(
        "Membership overlap blocked: "
        f"{'yes' if result.overlap_blocked else 'no'}"
    )
    print(
        "Delisted security retained: "
        f"{'yes' if result.delisted_security_retained else 'no'}"
    )
    if not all(
        (
            result.snapshot_digest_match,
            result.context_digest_match,
            result.future_data_blocked,
            result.overlap_blocked,
            result.delisted_security_retained,
            result.member_count == 2,
        )
    ):
        raise RuntimeError("Point-in-time integrity simulation failed.")


if __name__ == "__main__":
    main()
