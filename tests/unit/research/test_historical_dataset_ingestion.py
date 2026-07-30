from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from world_quant_system.domain import CandleInterval
from world_quant_system.research import (
    HistoricalDatasetEligibilityError,
    HistoricalDatasetFrozenError,
    HistoricalDatasetImporter,
    HistoricalDatasetImportSpec,
    HistoricalDatasetIntegrityError,
    HistoricalDatasetPolicy,
    HistoricalDatasetState,
    MissingSessionPolicy,
    SQLiteHistoricalDatasetStore,
    parse_historical_csv,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
HEADER = "timestamp,symbol,open,high,low,close,volume,currency\n"
ROWS = (
    "2020-01-02T09:00:00+09:00,005930,55000,56000,54500,55500,1000,KRW\n"
    "2020-01-03T09:00:00+09:00,005930,55500,56500,55000,56000,1100,KRW\n"
    "2020-01-06T09:00:00+09:00,005930,56000,57000,55500,56500,1200,KRW\n"
)


def spec(
    *,
    symbol: str = "005930",
    missing: MissingSessionPolicy = MissingSessionPolicy.REJECT,
) -> HistoricalDatasetImportSpec:
    return HistoricalDatasetImportSpec(
        provider="localcsv",
        exchange="KRX",
        symbol=symbol,
        interval=CandleInterval.DAY_1,
        currency="KRW",
        code_commit="a1b2c3d",
        point_in_time_context_digest=SHA_A,
        corporate_action_context_digest=SHA_B,
        policy=HistoricalDatasetPolicy(
            timezone="Asia/Seoul",
            missing_session_policy=missing,
            holidays=(date(2020, 1, 1),),
        ),
    )


def write_csv(path: Path, rows: str = ROWS) -> Path:
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_import_is_idempotent_and_verifiable(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "source.csv")
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    importer = HistoricalDatasetImporter(store)

    first = await importer.import_csv(source, spec())
    second = await importer.import_csv(source, spec())
    verified = await store.verify(first.manifest.dataset_id)

    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert first.dataset_digest == second.dataset_digest
    assert verified.dataset_digest == first.dataset_digest
    assert first.manifest.item_count == 3


@pytest.mark.asyncio
async def test_digest_matches_across_independent_catalogs(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "source.csv")
    first = await HistoricalDatasetImporter(
        SQLiteHistoricalDatasetStore(tmp_path / "one")
    ).import_csv(source, spec())
    second = await HistoricalDatasetImporter(
        SQLiteHistoricalDatasetStore(tmp_path / "two")
    ).import_csv(source, spec())

    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert first.dataset_digest == second.dataset_digest


@pytest.mark.asyncio
async def test_freeze_is_idempotent(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "source.csv")
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    imported = await HistoricalDatasetImporter(store).import_csv(source, spec())
    first = await store.freeze(imported.manifest.dataset_id)
    second = await store.freeze(imported.manifest.dataset_id)

    assert first == second
    assert first.state is HistoricalDatasetState.FROZEN


def test_parser_rejects_duplicate_or_reversed_timestamps(tmp_path: Path) -> None:
    duplicate = (
        "2020-01-02T09:00:00+09:00,005930,1,1,1,1,1,KRW\n"
        "2020-01-02T09:00:00+09:00,005930,1,1,1,1,1,KRW\n"
    )
    source = write_csv(tmp_path / "duplicate.csv", duplicate)
    with pytest.raises(HistoricalDatasetEligibilityError):
        parse_historical_csv(source, spec())


def test_parser_rejects_timezone_offset_mismatch(tmp_path: Path) -> None:
    rows = "2020-01-02T00:00:00+00:00,005930,1,1,1,1,1,KRW\n"
    source = write_csv(tmp_path / "utc.csv", rows)
    with pytest.raises(HistoricalDatasetEligibilityError, match="offset"):
        parse_historical_csv(source, spec())


def test_parser_rejects_mixed_symbol(tmp_path: Path) -> None:
    rows = "2020-01-02T09:00:00+09:00,000660,1,1,1,1,1,KRW\n"
    source = write_csv(tmp_path / "mixed.csv", rows)
    with pytest.raises(HistoricalDatasetEligibilityError, match="symbol"):
        parse_historical_csv(source, spec())


def test_missing_session_can_warn_or_reject(tmp_path: Path) -> None:
    rows = (
        "2020-01-02T09:00:00+09:00,005930,1,1,1,1,1,KRW\n"
        "2020-01-06T09:00:00+09:00,005930,1,1,1,1,1,KRW\n"
    )
    source = write_csv(tmp_path / "gap.csv", rows)
    with pytest.raises(HistoricalDatasetEligibilityError, match="2020-01-03"):
        parse_historical_csv(source, spec())

    parsed = parse_historical_csv(
        source,
        spec(missing=MissingSessionPolicy.WARN),
    )
    assert len(parsed.issues) == 1
    assert parsed.issues[0].session_date == date(2020, 1, 3)


@pytest.mark.asyncio
async def test_manifest_tampering_is_detected(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "source.csv")
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    snapshot = await HistoricalDatasetImporter(store).import_csv(source, spec())
    manifest = (
        store.root / "manifests" / f"{snapshot.manifest.dataset_id}.json"
    )
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(HistoricalDatasetIntegrityError):
        await store.get_snapshot(snapshot.manifest.dataset_id)


@pytest.mark.asyncio
async def test_catalog_metadata_tampering_is_detected(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "source.csv")
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    snapshot = await HistoricalDatasetImporter(store).import_csv(source, spec())
    database = store.root / "historical_datasets.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE datasets SET dataset_digest = ? WHERE dataset_id = ?",
            ("f" * 64, snapshot.manifest.dataset_id),
        )
        connection.commit()

    with pytest.raises(HistoricalDatasetIntegrityError):
        await store.get_snapshot(snapshot.manifest.dataset_id)


@pytest.mark.asyncio
async def test_benchmark_link_requires_frozen_aligned_datasets(tmp_path: Path) -> None:
    source = write_csv(tmp_path / "strategy.csv")
    benchmark_rows = ROWS.replace("005930", "KOSPI")
    benchmark_source = write_csv(tmp_path / "benchmark.csv", benchmark_rows)
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    importer = HistoricalDatasetImporter(store)
    strategy = await importer.import_csv(source, spec())
    benchmark = await importer.import_csv(benchmark_source, spec(symbol="KOSPI"))

    with pytest.raises(HistoricalDatasetFrozenError, match="frozen"):
        await store.link_benchmark(
            strategy.manifest.dataset_id,
            benchmark.manifest.dataset_id,
        )

    await store.freeze(strategy.manifest.dataset_id)
    await store.freeze(benchmark.manifest.dataset_id)
    link = await store.link_benchmark(
        strategy.manifest.dataset_id,
        benchmark.manifest.dataset_id,
    )
    repeated = await store.link_benchmark(
        strategy.manifest.dataset_id,
        benchmark.manifest.dataset_id,
    )
    assert link == repeated


@pytest.mark.asyncio
async def test_same_source_content_with_new_filename_is_idempotent(
    tmp_path: Path,
) -> None:
    first_source = write_csv(tmp_path / "first.csv")
    second_source = write_csv(tmp_path / "renamed.csv")
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    importer = HistoricalDatasetImporter(store)

    first = await importer.import_csv(first_source, spec())
    second = await importer.import_csv(second_source, spec())

    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert first.dataset_digest == second.dataset_digest


def test_invalid_ohlc_is_reported_as_dataset_eligibility_failure(
    tmp_path: Path,
) -> None:
    rows = "2020-01-02T09:00:00+09:00,005930,10,9,8,9,1,KRW\n"
    source = write_csv(tmp_path / "invalid.csv", rows)
    with pytest.raises(HistoricalDatasetEligibilityError, match="candle invariants"):
        parse_historical_csv(source, spec())


@pytest.mark.asyncio
async def test_benchmark_link_rejects_different_session_timestamps(
    tmp_path: Path,
) -> None:
    strategy_source = write_csv(tmp_path / "strategy.csv")
    benchmark_rows = (
        "2020-01-02T09:00:00+09:00,KOSPI,55000,56000,54500,55500,1000,KRW\n"
        "2020-01-03T10:00:00+09:00,KOSPI,55500,56500,55000,56000,1100,KRW\n"
        "2020-01-06T09:00:00+09:00,KOSPI,56000,57000,55500,56500,1200,KRW\n"
    )
    benchmark_source = write_csv(tmp_path / "benchmark.csv", benchmark_rows)
    store = SQLiteHistoricalDatasetStore(tmp_path / "catalog")
    importer = HistoricalDatasetImporter(store)
    strategy = await importer.import_csv(strategy_source, spec())
    benchmark = await importer.import_csv(benchmark_source, spec(symbol="KOSPI"))
    await store.freeze(strategy.manifest.dataset_id)
    await store.freeze(benchmark.manifest.dataset_id)

    with pytest.raises(HistoricalDatasetEligibilityError, match="sessions"):
        await store.link_benchmark(
            strategy.manifest.dataset_id,
            benchmark.manifest.dataset_id,
        )
