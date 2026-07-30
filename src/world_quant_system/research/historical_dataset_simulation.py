from __future__ import annotations

import asyncio
import tempfile
from datetime import date
from pathlib import Path

from world_quant_system.domain import CandleInterval
from world_quant_system.research.historical_dataset_ingestion import (
    HistoricalDatasetImporter,
)
from world_quant_system.research.historical_dataset_models import (
    HistoricalDatasetImportSpec,
    HistoricalDatasetPolicy,
    HistoricalDatasetState,
    MissingSessionPolicy,
)
from world_quant_system.research.historical_dataset_store import (
    SQLiteHistoricalDatasetStore,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


async def run_historical_dataset_simulation() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "samsung.csv"
        source.write_text(
            "timestamp,symbol,open,high,low,close,volume,currency\n"
            "2020-01-02T09:00:00+09:00,005930,55000,56000,54500,55500,1000,KRW\n"
            "2020-01-03T09:00:00+09:00,005930,55500,56500,55000,56000,1100,KRW\n"
            "2020-01-06T09:00:00+09:00,005930,56000,57000,55500,56500,1200,KRW\n",
            encoding="utf-8",
        )
        spec = HistoricalDatasetImportSpec(
            provider="localcsv",
            exchange="KRX",
            symbol="005930",
            interval=CandleInterval.DAY_1,
            currency="KRW",
            code_commit="a1b2c3d",
            point_in_time_context_digest=_DIGEST_A,
            corporate_action_context_digest=_DIGEST_B,
            policy=HistoricalDatasetPolicy(
                timezone="Asia/Seoul",
                missing_session_policy=MissingSessionPolicy.REJECT,
                holidays=(date(2020, 1, 1),),
            ),
        )

        first_store = SQLiteHistoricalDatasetStore(root / "catalog-a")
        first_importer = HistoricalDatasetImporter(first_store)
        first = await first_importer.import_csv(source, spec)
        repeated = await first_importer.import_csv(source, spec)
        frozen = await first_store.freeze(first.manifest.dataset_id)
        verified = await first_store.verify(first.manifest.dataset_id)

        second_store = SQLiteHistoricalDatasetStore(root / "catalog-b")
        second = await HistoricalDatasetImporter(second_store).import_csv(source, spec)

        assert first.manifest.dataset_id == repeated.manifest.dataset_id
        assert first.dataset_digest == repeated.dataset_digest
        assert first.dataset_digest == second.dataset_digest
        assert frozen.state is HistoricalDatasetState.FROZEN
        assert verified.state is HistoricalDatasetState.FROZEN

        return {
            "dataset_id": first.manifest.dataset_id,
            "dataset_digest": first.dataset_digest,
            "items": first.manifest.item_count,
            "idempotent_import": True,
            "deterministic_cross_catalog_digest": True,
            "frozen": True,
            "network_used": False,
        }


def main() -> None:
    result = asyncio.run(run_historical_dataset_simulation())
    print(f"Historical dataset items: {result['items']}")
    print(f"Dataset ID: {result['dataset_id']}")
    print(f"Dataset digest: {result['dataset_digest']}")
    print("Idempotent import: yes")
    print("Cross-catalog deterministic digest: match")
    print("Frozen snapshot: yes")
    print("Network access: disabled")


if __name__ == "__main__":
    main()
