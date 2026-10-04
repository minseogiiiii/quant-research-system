from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from world_quant_system.data.normalized_models import (
    NormalizedMarketDataConflictError,
    NormalizedMarketDataRejectedError,
)
from world_quant_system.data.normalized_store import SQLiteNormalizedMarketDataStore
from world_quant_system.data.quality_models import (
    DataQualityReport,
    QualityDatasetKind,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    QualityStatus,
)
from world_quant_system.domain.models import Candle, CandleInterval, CandlePage
from world_quant_system.replay import DeterministicReplayEngine, ReplayConfig


@dataclass(frozen=True, slots=True)
class NormalizedReplayBacktestResult:
    normalized_items: int
    replayed_items: int
    warning_items_retained: int
    pass_only_items: int
    duplicate_writes_idempotent: bool
    conflicting_rewrite_blocked: bool
    quarantine_blocked: bool
    deterministic_digest_match: bool

    @property
    def passed(self) -> bool:
        return (
            self.normalized_items == self.replayed_items
            and self.warning_items_retained > 0
            and self.pass_only_items < self.replayed_items
            and self.duplicate_writes_idempotent
            and self.conflicting_rewrite_blocked
            and self.quarantine_blocked
            and self.deterministic_digest_match
        )


def run_normalized_replay_backtest() -> NormalizedReplayBacktestResult:
    return asyncio.run(_run_backtest())


async def _run_backtest() -> NormalizedReplayBacktestResult:
    base = datetime(2020, 1, 1, tzinfo=UTC)
    pass_candles = _candles(
        symbol="005930",
        start=base,
        count=1_500,
        start_price=Decimal("50000"),
    )
    warning_candles = _candles(
        symbol="000660",
        start=base,
        count=500,
        start_price=Decimal("80000"),
    )

    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteNormalizedMarketDataStore(Path(directory) / "normalized")
        pass_report = _report(
            item_count=len(pass_candles),
            status=QualityStatus.PASS,
        )
        warning_report = _report(
            item_count=len(warning_candles),
            status=QualityStatus.WARNING,
        )
        normalized_at = datetime(2026, 7, 21, tzinfo=UTC)

        pass_records = await store.save_candle_page(
            CandlePage(pass_candles, None),
            pass_report,
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )
        warning_records = await store.save_candle_page(
            CandlePage(warning_candles, None),
            warning_report,
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )

        duplicate_records = await store.save_candle_page(
            CandlePage(pass_candles, None),
            pass_report,
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )
        duplicate_writes_idempotent = (
            tuple(record.item_id for record in duplicate_records)
            == tuple(record.item_id for record in pass_records)
        )

        conflicting_rewrite_blocked = await _conflict_is_blocked(
            store,
            pass_candles[0],
            normalized_at,
        )
        quarantine_blocked = await _quarantine_is_blocked(
            store,
            pass_candles[0],
            normalized_at,
        )

        all_config = ReplayConfig(
            symbols=("005930", "000660"),
            interval=CandleInterval.DAY_1,
            include_warnings=True,
            page_size=127,
        )
        first = await DeterministicReplayEngine(store, all_config).run()
        second = await DeterministicReplayEngine(store, all_config).run()
        alternate_page_size = await DeterministicReplayEngine(
            store,
            ReplayConfig(
                symbols=("005930", "000660"),
                interval=CandleInterval.DAY_1,
                include_warnings=True,
                page_size=211,
            ),
        ).run()
        pass_only = await DeterministicReplayEngine(
            store,
            ReplayConfig(
                symbols=("005930", "000660"),
                interval=CandleInterval.DAY_1,
                include_warnings=False,
                page_size=113,
            ),
        ).run()

        return NormalizedReplayBacktestResult(
            normalized_items=len(pass_records) + len(warning_records),
            replayed_items=first.event_count,
            warning_items_retained=first.warning_count,
            pass_only_items=pass_only.event_count,
            duplicate_writes_idempotent=duplicate_writes_idempotent,
            conflicting_rewrite_blocked=conflicting_rewrite_blocked,
            quarantine_blocked=quarantine_blocked,
            deterministic_digest_match=(
                first.event_digest
                == second.event_digest
                == alternate_page_size.event_digest
            ),
        )


async def _conflict_is_blocked(
    store: SQLiteNormalizedMarketDataStore,
    candle: Candle,
    normalized_at: datetime,
) -> bool:
    changed = Candle(
        symbol=candle.symbol,
        interval=candle.interval,
        timestamp=candle.timestamp,
        open_price=candle.open_price,
        high_price=candle.high_price + Decimal("1000"),
        low_price=candle.low_price,
        close_price=candle.close_price + Decimal("500"),
        volume=candle.volume,
        currency=candle.currency,
        source=candle.source,
    )
    try:
        await store.save_candle_page(
            CandlePage((changed,), None),
            _report(item_count=1, status=QualityStatus.PASS),
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )
    except NormalizedMarketDataConflictError:
        return True
    return False


async def _quarantine_is_blocked(
    store: SQLiteNormalizedMarketDataStore,
    candle: Candle,
    normalized_at: datetime,
) -> bool:
    try:
        await store.save_candle_page(
            CandlePage((candle,), None),
            _report(item_count=1, status=QualityStatus.QUARANTINE),
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )
    except NormalizedMarketDataRejectedError:
        return True
    return False


def _candles(
    *,
    symbol: str,
    start: datetime,
    count: int,
    start_price: Decimal,
) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    for index in range(count):
        close = start_price + Decimal(index % 97)
        candles.append(
            Candle(
                symbol=symbol,
                interval=CandleInterval.DAY_1,
                timestamp=start + timedelta(days=index),
                open_price=close,
                high_price=close + Decimal("10"),
                low_price=close - Decimal("10"),
                close_price=close,
                volume=1_000_000 + index,
                currency="KRW",
                source="toss",
            )
        )
    return tuple(candles)


def _report(*, item_count: int, status: QualityStatus) -> DataQualityReport:
    raw_sha = hashlib.sha256(str(uuid4()).encode()).hexdigest()
    issues: tuple[QualityIssue, ...] = ()
    if status is QualityStatus.WARNING:
        issues = (
            QualityIssue(
                code=QualityIssueCode.EXTREME_PRICE_MOVE,
                severity=QualitySeverity.WARNING,
                message="Synthetic alpha-preservation warning.",
            ),
        )
    elif status is QualityStatus.QUARANTINE:
        issues = (
            QualityIssue(
                code=QualityIssueCode.RAW_INTEGRITY_FAILURE,
                severity=QualitySeverity.QUARANTINE,
                message="Synthetic unsafe record.",
            ),
        )
    return DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=raw_sha,
        dataset_kind=QualityDatasetKind.CANDLES,
        status=status,
        checked_at=datetime(2026, 7, 21, tzinfo=UTC),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=item_count,
        issues=issues,
    )
