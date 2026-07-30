import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.research import (
    CorporateActionConflictError,
    CorporateActionEligibilityError,
    CorporateActionIntegrityError,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    SQLiteCorporateActionStore,
)

START = datetime(2020, 1, 1, tzinfo=UTC)
END = START + timedelta(days=20)


def split_action() -> CorporateActionRecord:
    return CorporateActionRecord(
        exchange="XKRX",
        symbol="005930",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=5),
        available_at=START,
        source="test",
        source_digest="a" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )


@pytest.mark.asyncio
async def test_action_writes_are_idempotent_and_conflicts_fail(
    tmp_path: Path,
) -> None:
    store = SQLiteCorporateActionStore(tmp_path / "actions")
    candidate = split_action()

    results = await asyncio.gather(
        *(store.save_action(candidate) for _ in range(20))
    )

    assert all(result == candidate for result in results)
    with pytest.raises(CorporateActionConflictError):
        await store.save_action(
            CorporateActionRecord(
                exchange="XKRX",
                symbol="005930",
                action_type=CorporateActionType.SPLIT,
                effective_at=START + timedelta(days=5),
                available_at=START,
                source="test",
                source_digest="b" * 64,
                ratio_numerator=3,
                ratio_denominator=1,
            )
        )


@pytest.mark.asyncio
async def test_context_follows_symbol_change_and_requires_delisting(
    tmp_path: Path,
) -> None:
    store = SQLiteCorporateActionStore(tmp_path / "actions")
    symbol_change = CorporateActionRecord(
        exchange="XKRX",
        symbol="OLD",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        effective_at=START + timedelta(days=5),
        available_at=START,
        source="test",
        source_digest="c" * 64,
        new_symbol="NEW",
    )
    delisting = CorporateActionRecord(
        exchange="XKRX",
        symbol="NEW",
        action_type=CorporateActionType.DELISTING,
        effective_at=START + timedelta(days=10),
        available_at=START + timedelta(days=7),
        source="test",
        source_digest="d" * 64,
        delisting_cash_price=Decimal("0"),
    )
    await store.save_action(symbol_change)
    await store.save_action(delisting)

    context = await store.build_backtest_context(
        exchange="XKRX",
        initial_symbol="OLD",
        start=START,
        end=END,
        expected_delisted_at=delisting.effective_at,
    )
    actions = await store.load_context_actions(context)

    assert context.symbols == ("OLD", "NEW")
    assert tuple(action.action_type for action in actions) == (
        CorporateActionType.SYMBOL_CHANGE,
        CorporateActionType.DELISTING,
    )


@pytest.mark.asyncio
async def test_future_known_action_is_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    store = SQLiteCorporateActionStore(tmp_path / "actions")
    action = CorporateActionRecord(
        exchange="XKRX",
        symbol="005930",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=5),
        available_at=START + timedelta(days=6),
        source="test",
        source_digest="e" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    await store.save_action(action)

    with pytest.raises(CorporateActionEligibilityError):
        await store.build_backtest_context(
            exchange="XKRX",
            initial_symbol="005930",
            start=START,
            end=END,
            policy=CorporateActionPolicy(require_known_before_event=True),
        )


@pytest.mark.asyncio
async def test_store_tampering_is_detected(tmp_path: Path) -> None:
    store = SQLiteCorporateActionStore(tmp_path / "actions")
    action = split_action()
    await store.save_action(action)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE corporate_actions SET action_json = '{}' WHERE action_id = ?",
            (action.action_id,),
        )

    with pytest.raises(CorporateActionIntegrityError):
        await store.get_action(action.action_id)

@pytest.mark.asyncio
async def test_context_rejects_action_on_retired_symbol(
    tmp_path: Path,
) -> None:
    store = SQLiteCorporateActionStore(tmp_path / "actions")
    symbol_change = CorporateActionRecord(
        exchange="XKRX",
        symbol="OLD",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        effective_at=START + timedelta(days=5),
        available_at=START,
        source="test",
        source_digest="f" * 64,
        new_symbol="NEW",
    )
    stale_split = CorporateActionRecord(
        exchange="XKRX",
        symbol="OLD",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=6),
        available_at=START,
        source="test",
        source_digest="0" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    await store.save_action(symbol_change)
    await store.save_action(stale_split)

    with pytest.raises(
        CorporateActionIntegrityError,
        match="retired symbol",
    ):
        await store.build_backtest_context(
            exchange="XKRX",
            initial_symbol="OLD",
            start=START,
            end=END,
        )
