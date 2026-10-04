#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

unset PYTHONHOME PYTHONPATH

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    echo "ERROR: uv was not found in PATH."
    exit 1
fi

if [[ ! -f uv.lock ]]; then
    echo "ERROR: uv.lock is missing. Run: uv lock"
    exit 1
fi

if grep -qE 'applied-caas|internal\.api\.openai\.org' uv.lock; then
    echo "ERROR: uv.lock contains a private package index."
    exit 1
fi

# Prevent user-level uv settings from silently skipping the local project.
unset UV_NO_INSTALL_LOCAL UV_NO_INSTALL_PROJECT UV_NO_INSTALL_WORKSPACE
unset UV_NO_PROJECT UV_NO_SYNC UV_NO_EDITABLE UV_PROJECT_ENVIRONMENT
unset UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX

"$UV_BIN" sync \
    --locked \
    --no-editable \
    --reinstall-package world-quant-system \
    --default-index https://pypi.org/simple

PYTHON="$ROOT_DIR/.venv/bin/python"
RUFF="$ROOT_DIR/.venv/bin/ruff"
MYPY="$ROOT_DIR/.venv/bin/mypy"
CONSOLE="$ROOT_DIR/.venv/bin/world-quant-system"
WQS="$ROOT_DIR/.venv/bin/wqs"

for executable in "$PYTHON" "$RUFF" "$MYPY" "$CONSOLE" "$WQS"; do
    if [[ ! -x "$executable" ]]; then
        echo "ERROR: Required executable is missing: $executable"
        exit 1
    fi
done

echo "1/18 Verifying the installed package outside the repository..."
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PYTHON" -c '
import world_quant_system
from world_quant_system.backtest import (
    AtomicJsonBacktestSummaryWriter,
    BacktestConfig,
    BuyAndHoldStrategy,
    CorporateActionTimeline,
    FlatRateDividendTaxModel,
    NextOpenExecutionModel,
    SmaCrossoverStrategy,
    StrategyBacktestEngine,
)
from world_quant_system.adapters.toss import (
    HttpMethod,
    TossAdapterError,
    TossHttpClient,
    TossMarketDataParser,
    TossMarketDataProvider,
    TokenIssueResponse,
    TossRequest,
    TossResponse,
    TossTokenManager,
)
from world_quant_system.data import (
    DataQualityValidator,
    FileRawMarketDataStore,
    MarketDataNormalizer,
    MarketDataQualityGate,
    NormalizedCandleRecord,
    QualityStatus,
    RawMarketDataCapture,
    SQLiteDataQualityStore,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage
from world_quant_system.replay import DeterministicReplayEngine, ReplayConfig
from world_quant_system.research import (
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    DataAvailabilityRecord,
    ExperimentOutcome,
    ExperimentSpec,
    ExperimentStatus,
    HoldoutConsumption,
    HistoricalDatasetImportSpec,
    HistoricalDatasetImporter,
    HistoricalDatasetPolicy,
    HistoricalDatasetSnapshot,
    MissingSessionPolicy,
    ParameterSearchAudit,
    PointInTimePolicy,
    PointInTimeSnapshot,
    ResearchSplit,
    ResearchWindow,
    SQLiteCorporateActionStore,
    SQLiteExperimentRegistry,
    SQLiteHistoricalDatasetStore,
    SQLitePointInTimeStore,
    SecurityLifecycle,
    StatisticalReturnsMatrix,
    StatisticalValidationPolicy,
    StatisticalValidationReport,
    UniverseMembership,
)
from world_quant_system.research.robustness import (
    DeterministicRobustnessRunner,
    StandardBacktestRunFactory,
    build_stress_scenarios,
)
from world_quant_system.research.robustness_models import (
    RobustnessPolicy,
    RobustnessReport,
    WalkForwardPlan,
)
from world_quant_system.research.statistical_validation import (
    DeterministicStatisticalValidator,
)

print(f"Package: {world_quant_system.__file__}")
print(f"Client: {TossHttpClient.__name__}")
print(f"Token manager: {TossTokenManager.__name__}")
print(
    "Market data: "
    f"{TossMarketDataProvider.__name__}, "
    f"{TossMarketDataParser.__name__}"
)
print(f"Domain: {Candle.__name__}, {CandleInterval.__name__}, {CandlePage.__name__}")
print(
    "Raw storage: "
    f"{FileRawMarketDataStore.__name__}, "
    f"{RawMarketDataCapture.__name__}"
)
print(
    "Quality gate: "
    f"{MarketDataQualityGate.__name__}, "
    f"{DataQualityValidator.__name__}, "
    f"{SQLiteDataQualityStore.__name__}, "
    f"{QualityStatus.__name__}"
)
print(
    "Normalized data: "
    f"{MarketDataNormalizer.__name__}, "
    f"{SQLiteNormalizedMarketDataStore.__name__}, "
    f"{NormalizedCandleRecord.__name__}"
)
print(
    "Replay: "
    f"{DeterministicReplayEngine.__name__}, "
    f"{ReplayConfig.__name__}"
)
print(
    "Backtest: "
    f"{StrategyBacktestEngine.__name__}, "
    f"{BacktestConfig.__name__}, "
    f"{NextOpenExecutionModel.__name__}, "
    f"{BuyAndHoldStrategy.__name__}, "
    f"{SmaCrossoverStrategy.__name__}, "
    f"{AtomicJsonBacktestSummaryWriter.__name__}"
)
print(
    "Research validity: "
    f"{ResearchSplit.__name__}, "
    f"{ExperimentSpec.__name__}, "
    f"{ParameterSearchAudit.__name__}, "
    f"{ExperimentOutcome.__name__}, "
    f"{ExperimentStatus.__name__}, "
    f"{HoldoutConsumption.__name__}, "
    f"{SQLiteExperimentRegistry.__name__}"
)
print(
    "Historical datasets: "
    f"{HistoricalDatasetImportSpec.__name__}, "
    f"{HistoricalDatasetPolicy.__name__}, "
    f"{HistoricalDatasetSnapshot.__name__}, "
    f"{HistoricalDatasetImporter.__name__}, "
    f"{SQLiteHistoricalDatasetStore.__name__}, "
    f"{MissingSessionPolicy.__name__}"
)
print(
    "Robustness validation: "
    f"{WalkForwardPlan.__name__}, "
    f"{RobustnessPolicy.__name__}, "
    f"{RobustnessReport.__name__}, "
    f"{DeterministicRobustnessRunner.__name__}, "
    f"{StandardBacktestRunFactory.__name__}, "
    f"{build_stress_scenarios.__name__}"
)
print(
    "Statistical validation: "
    f"{StatisticalReturnsMatrix.__name__}, "
    f"{StatisticalValidationPolicy.__name__}, "
    f"{StatisticalValidationReport.__name__}, "
    f"{DeterministicStatisticalValidator.__name__}"
)
print(
    "Corporate actions: "
    f"{CorporateActionRecord.__name__}, "
    f"{CorporateActionType.__name__}, "
    f"{CorporateActionPolicy.__name__}, "
    f"{SQLiteCorporateActionStore.__name__}, "
    f"{CorporateActionTimeline.__name__}, "
    f"{FlatRateDividendTaxModel.__name__}"
)
print(
    "Point-in-time data: "
    f"{SecurityLifecycle.__name__}, "
    f"{UniverseMembership.__name__}, "
    f"{DataAvailabilityRecord.__name__}, "
    f"{PointInTimePolicy.__name__}, "
    f"{PointInTimeSnapshot.__name__}, "
    f"{SQLitePointInTimeStore.__name__}"
)
print(f"Token response: {TokenIssueResponse.__name__}")
print(f"Error: {TossAdapterError.__name__}")
print(
    "Schemas: "
    f"{HttpMethod.__name__}, "
    f"{TossRequest.__name__}, "
    f"{TossResponse.__name__}"
)
'
)

echo "2/18 Compiling source and tests..."
"$PYTHON" -m compileall -q src tests

echo "3/18 Running tests..."
"$PYTHON" -m pytest -q

echo "4/18 Running Ruff..."
rm -rf build dist
"$RUFF" check .

echo "5/18 Running mypy..."
"$MYPY" src tests

echo "6/18 Verifying fail-closed network behavior..."
"$PYTHON" -c '
import asyncio
import tempfile
from datetime import UTC, datetime

from world_quant_system.backtest import (
    AtomicJsonBacktestSummaryWriter,
    BacktestConfig,
    BuyAndHoldStrategy,
    NextOpenExecutionModel,
    SmaCrossoverStrategy,
    StrategyBacktestEngine,
)
from world_quant_system.adapters.toss import (
    TossHttpClient,
    TossMarketDataProvider,
    TossTokenManager,
    TossTransportError,
)
from world_quant_system.data import (
    FileRawMarketDataStore,
    MarketDataNormalizer,
    MarketDataQualityGate,
    QualityStatus,
    RawMarketDataCapture,
    SQLiteDataQualityStore,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage
from world_quant_system.replay import DeterministicReplayEngine, ReplayConfig
from decimal import Decimal

async def verify() -> None:
    client = TossHttpClient("https://example.test")
    try:
        await client.get("/prices")
    except TossTransportError as error:
        assert str(error) == "Network transport is not configured."
    else:
        raise AssertionError("Default transport unexpectedly allowed a request.")

    provider = TossMarketDataProvider(client)
    try:
        await provider.get_quote("005930")
    except TossTransportError as error:
        assert str(error) == "Network transport is not configured."
    else:
        raise AssertionError("Market-data provider unexpectedly allowed a request.")

    manager = TossTokenManager()
    try:
        await manager.get_token()
    except TossTransportError as error:
        assert str(error) == "Token issuer is not configured."
    else:
        raise AssertionError("Default token issuer unexpectedly issued a token.")

    with tempfile.TemporaryDirectory() as directory:
        store = FileRawMarketDataStore(directory)
        metadata = await store.record(
            RawMarketDataCapture(
                provider="toss",
                endpoint="/api/v1/prices",
                request_params={"symbols": "005930"},
                captured_at=datetime.now(UTC),
                status_code=200,
                response_headers={"X-Request-Id": "check-script"},
                json_body={"result": []},
                request_id="check-script",
                idempotency_key="check-script",
            )
        )
        record = await store.read(metadata.record_id)
        assert record.metadata == metadata
        assert record.json_body == {"result": []}

        quality_store = SQLiteDataQualityStore(f"{directory}/quality")
        quality_gate = MarketDataQualityGate(store, quality_store)
        report = await quality_gate.assess_parse_failure(
            metadata,
            expected_endpoint="/api/v1/prices",
        )
        assert report.status is QualityStatus.QUARANTINE
        stored_report = await quality_store.get(report.report_id)
        assert stored_report == report

        normalized_store = SQLiteNormalizedMarketDataStore(
            f"{directory}/normalized"
        )
        normalizer = MarketDataNormalizer(normalized_store)
        candle = Candle(
            symbol="005930",
            interval=CandleInterval.DAY_1,
            timestamp=datetime(2026, 7, 20, tzinfo=UTC),
            open_price=Decimal("95000"),
            high_price=Decimal("96000"),
            low_price=Decimal("94000"),
            close_price=Decimal("95500"),
            volume=1000,
            currency="KRW",
            source="toss",
        )
        candle_metadata = await store.record(
            RawMarketDataCapture(
                provider="toss",
                endpoint="/api/v1/candles",
                request_params={"symbol": "005930", "interval": "1d"},
                captured_at=datetime(2026, 7, 21, tzinfo=UTC),
                status_code=200,
                response_headers={"X-Request-Id": "check-candle"},
                json_body={"result": []},
                request_id="check-candle",
                idempotency_key="check-candle",
            )
        )
        candle_report = await quality_gate.assess_candle_page(
            candle_metadata,
            CandlePage((candle,), None),
        )
        assert candle_report.status is not QualityStatus.QUARANTINE
        records = await normalizer.normalize_candle_page(
            candle_report,
            CandlePage((candle,), None),
        )
        assert len(records) == 1
        replay = await DeterministicReplayEngine(
            normalized_store,
            ReplayConfig(
                symbols=("005930",),
                interval=CandleInterval.DAY_1,
            ),
        ).run()
        assert replay.event_count == 1

asyncio.run(verify())
'

echo "7/18 Running deterministic data-quality simulation..."
"$PYTHON" scripts/quality_gate_backtest.py

echo "8/18 Running normalized-storage replay simulation..."
"$PYTHON" scripts/normalized_replay_backtest.py

echo "9/18 Running deterministic strategy backtest simulation..."
"$PYTHON" scripts/strategy_backtest.py

echo "10/18 Running deterministic research-validity simulation..."
"$PYTHON" scripts/research_validity_simulation.py

echo "11/18 Running point-in-time data-integrity simulation..."
"$PYTHON" scripts/point_in_time_simulation.py

echo "12/18 Running corporate-action and delisting-economics simulation..."
"$PYTHON" scripts/corporate_action_simulation.py

echo "13/18 Running historical-dataset integrity simulation..."
"$PYTHON" scripts/historical_dataset_simulation.py

echo "14/18 Running robustness and walk-forward simulation..."
"$PYTHON" scripts/robustness_validation_simulation.py

echo "15/18 Running statistical-validation and overfitting simulation..."
"$PYTHON" scripts/statistical_validation_simulation.py

echo "16/18 Testing module entry point..."
MODULE_OUTPUT="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 \
        "$PYTHON" -m world_quant_system
)"

echo "17/18 Testing console entry point..."
CONSOLE_OUTPUT="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$CONSOLE"
)"

if [[ "$MODULE_OUTPUT" != "$CONSOLE_OUTPUT" ]]; then
    echo "ERROR: Module and console outputs differ."
    diff \
        <(printf '%s\n' "$MODULE_OUTPUT") \
        <(printf '%s\n' "$CONSOLE_OUTPUT") || true
    exit 1
fi

echo "18/18 Testing research CLI entry point..."
CLI_HELP="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$WQS" --help
)"
if [[ "$CLI_HELP" != *"backtest"* || "$CLI_HELP" != *"research"* \
    || "$CLI_HELP" != *"point-in-time"* \
    || "$CLI_HELP" != *"corporate-actions"* \
    || "$CLI_HELP" != *"dataset"* \
    || "$CLI_HELP" != *"robustness"* \
    || "$CLI_HELP" != *"statistical-validation"* ]]; then
    echo "ERROR: Research CLI does not expose required commands."
    exit 1
fi
RESEARCH_HELP="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$WQS" research --help
)"
for command in register inspect record-outcome consume-holdout; do
    if [[ "$RESEARCH_HELP" != *"$command"* ]]; then
        echo "ERROR: Research CLI does not expose $command."
        exit 1
    fi
done
DATASET_HELP="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$WQS" dataset --help
)"
for command in import validate freeze inspect link-benchmark list; do
    if [[ "$DATASET_HELP" != *"$command"* ]]; then
        echo "ERROR: Dataset CLI does not expose $command."
        exit 1
    fi
done
CORPORATE_ACTION_HELP="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$WQS" corporate-actions --help
)"
for command in register inspect list backtest-context; do
    if [[ "$CORPORATE_ACTION_HELP" != *"$command"* ]]; then
        echo "ERROR: Corporate-action CLI does not expose $command."
        exit 1
    fi
done
POINT_IN_TIME_HELP="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$WQS" point-in-time --help
)"
for command in register-security register-membership register-availability \
    register-delisting snapshot backtest-context inspect-security validate-access; do
    if [[ "$POINT_IN_TIME_HELP" != *"$command"* ]]; then
        echo "ERROR: Point-in-time CLI does not expose $command."
        exit 1
    fi
done

printf '%s\n' "$MODULE_OUTPUT"
echo
echo "All project checks passed."
