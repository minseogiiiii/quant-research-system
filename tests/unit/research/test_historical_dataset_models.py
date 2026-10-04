from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from world_quant_system.backtest import BacktestConfig
from world_quant_system.data import QualityStatus
from world_quant_system.domain import CandleInterval
from world_quant_system.replay import ReplayConfig
from world_quant_system.research import (
    BenchmarkLink,
    HistoricalDatasetConfigurationError,
    HistoricalDatasetEligibilityError,
    HistoricalDatasetFormat,
    HistoricalDatasetImportSpec,
    HistoricalDatasetIssue,
    HistoricalDatasetIssueCode,
    HistoricalDatasetManifest,
    HistoricalDatasetPolicy,
    MissingSessionPolicy,
)
from world_quant_system.research.historical_dataset_models import (
    canonical_manifest_json,
    historical_dataset_manifest_from_document,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
START = datetime(2020, 1, 2, tzinfo=UTC)
END = datetime(2020, 1, 6, tzinfo=UTC)


def make_spec(*, symbol: str = "005930") -> HistoricalDatasetImportSpec:
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
            holidays=(date(2020, 1, 1),),
        ),
    )


def make_manifest(
    *,
    symbol: str = "005930",
    raw_record_id: str | None = None,
    report_id: str | None = None,
    start: datetime = START,
    end: datetime = END,
) -> HistoricalDatasetManifest:
    spec = make_spec(symbol=symbol)
    return HistoricalDatasetManifest(
        dataset_id=spec.dataset_id(SHA_C),
        provider=spec.provider,
        exchange=spec.exchange,
        symbols=(symbol,),
        interval=spec.interval,
        timezone=spec.policy.timezone,
        currency=spec.currency,
        start=start,
        end=end,
        item_count=3,
        imported_at=end,
        source_format=HistoricalDatasetFormat.CSV,
        source_name="sample.csv",
        source_sha256=SHA_C,
        raw_record_id=raw_record_id or str(uuid4()),
        raw_content_sha256=SHA_D,
        quality_report_id=report_id or str(uuid4()),
        quality_status=QualityStatus.PASS,
        quality_policy_digest=SHA_A,
        normalizer_version="1.0.0",
        normalized_digest=SHA_B,
        point_in_time_context_digest=SHA_A,
        corporate_action_context_digest=SHA_B,
        code_commit="a1b2c3d",
        policy=spec.policy,
        issues=(
            HistoricalDatasetIssue(
                code=HistoricalDatasetIssueCode.MISSING_SESSION,
                message="warning",
                session_date=date(2020, 1, 3),
            ),
        ),
    )


def test_import_spec_produces_deterministic_dataset_id() -> None:
    spec = make_spec()
    assert spec.dataset_id(SHA_C) == spec.dataset_id(SHA_C)
    assert spec.dataset_id(SHA_C) != spec.dataset_id(SHA_D)


def test_policy_rejects_unknown_timezone() -> None:
    with pytest.raises(HistoricalDatasetConfigurationError):
        HistoricalDatasetPolicy(timezone="Mars/Olympus")


def test_policy_requires_sorted_unique_holidays() -> None:
    with pytest.raises(HistoricalDatasetConfigurationError):
        HistoricalDatasetPolicy(
            timezone="UTC",
            holidays=(date(2020, 1, 2), date(2020, 1, 1)),
        )


def test_manifest_round_trips_canonical_json() -> None:
    manifest = make_manifest()
    restored = historical_dataset_manifest_from_document(manifest.to_document())
    assert restored == manifest
    assert canonical_manifest_json(restored) == canonical_manifest_json(manifest)


def test_dataset_digest_excludes_random_storage_identifiers() -> None:
    first = make_manifest()
    second = make_manifest()
    assert first.manifest_digest != second.manifest_digest
    assert first.dataset_digest == second.dataset_digest


def test_manifest_rejects_quarantined_quality_status() -> None:
    manifest = make_manifest()
    values = manifest.to_document()
    values["quality_status"] = QualityStatus.QUARANTINE.value
    with pytest.raises(HistoricalDatasetEligibilityError):
        historical_dataset_manifest_from_document(values)


def test_benchmark_link_requires_exact_alignment() -> None:
    strategy = make_manifest()
    benchmark = make_manifest(symbol="KOSPI")
    link = BenchmarkLink.build(
        dataset=strategy,
        benchmark=benchmark,
        created_at=END,
    )
    assert link.dataset_id == strategy.dataset_id
    assert link.benchmark_dataset_id == benchmark.dataset_id

    misaligned = make_manifest(
        symbol="KOSDAQ",
        end=datetime(2020, 1, 7, tzinfo=UTC),
    )
    with pytest.raises(HistoricalDatasetEligibilityError):
        BenchmarkLink.build(
            dataset=strategy,
            benchmark=misaligned,
            created_at=END,
        )


def test_issue_policy_enum_is_strict() -> None:
    policy = HistoricalDatasetPolicy(
        timezone="UTC",
        missing_session_policy=MissingSessionPolicy.WARN,
    )
    assert policy.missing_session_policy is MissingSessionPolicy.WARN


def test_backtest_config_fingerprint_pins_historical_dataset_digest() -> None:
    replay = ReplayConfig(
        symbols=("005930",),
        interval=CandleInterval.DAY_1,
    )
    without_dataset = BacktestConfig(
        replay=replay,
        initial_cash=Decimal("1000000"),
    )
    with_dataset = BacktestConfig(
        replay=replay,
        initial_cash=Decimal("1000000"),
        historical_dataset_digest=SHA_C,
    )

    assert without_dataset.fingerprint != with_dataset.fingerprint
