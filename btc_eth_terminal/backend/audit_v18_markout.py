"""Frozen v18 future-label read-only gate for the original audited 1h source.

A reexecution of this diagnostic verifies historical source/reproducibility,
not live exchange execution, model generalization, or independent truth.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from audit_v16_one_hour import SOURCE_MANIFEST_SHA
from feature_label_partition import labels_visible_asof, partition
from post_quote_markout import MarkoutPolicy, inspect
from taker_quote_envelope import Assumptions, unique_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH"
INTENTS = ROOT / "examples/v17_predeclared_depth_intents.json"
POLICY = ROOT / "examples/v18_predeclared_markout_policy.json"
REPORT = ROOT / "P1_POST_QUOTE_MARKOUT_v18.json"
PARTITION = ROOT / "P1_ASOF_VS_FUTURE_PARTITION_v18.json"
INTENTS_SHA = "c7763f6107f332f83329210ca753861f53b5c0e62524481c899c9de05b29895c"
POLICY_SHA = "5ecc2d14b191a8b4ab235f5001aeeb15bd3e2a134edd88bd1f986912c1fb0691"
REPORT_SHA = "83c148e992b3b369e897b0f98cfa46dd7ee1c960f749aa42ff388be907d5b6c0"
PARTITION_SHA = "92e49adc089856eca490e08893d06fb56ebddae14a932a118b8fa33ad1ed1616"
EXPECTED_QUOTE_SEQUENCES = (1013, 20013, 30015, 60009, 80013, 100010, 150013)
EXPECTED_LABEL_SEQUENCES = (
    (1060, 1230, 2356),
    (20056, 20231, 21299),
    (30061, 30231, 31336),
    (60050, 60228, 61298),
    (80061, 80201, 81192),
    (100044, 100209, 101156),
    (150053, 150222, 151324),
)


class FrozenMarkoutError(ValueError):
    pass


def verify() -> dict:
    if sha256(INTENTS.read_bytes()).hexdigest() != INTENTS_SHA:
        raise FrozenMarkoutError("Prior frozen observation list mutated")
    if sha256(POLICY.read_bytes()).hexdigest() != POLICY_SHA:
        raise FrozenMarkoutError("Markout horizons/assumptions mutated")
    if sha256(REPORT.read_bytes()).hexdigest() != REPORT_SHA:
        raise FrozenMarkoutError("Saved label-only report mutated")
    if sha256(PARTITION.read_bytes()).hexdigest() != PARTITION_SHA:
        raise FrozenMarkoutError("As-of feature / future label partition mutated")
    params = unique_json(POLICY.read_bytes())
    if set(params) != {
        "horizons_ms",
        "first_observation_tolerance_ms",
        "latency_ms",
        "max_wait_ms",
        "max_signal_quote_age_ms",
        "one_way_taker_fee_bps",
        "extra_slippage_bps",
        "top_level_capacity_fraction",
    }:
        raise FrozenMarkoutError("Unexpected markout policy field")
    if params["horizons_ms"] != [1000, 5000, 30000]:
        raise FrozenMarkoutError("Changed labeled horizons")
    assumptions = Assumptions(
        params["latency_ms"],
        params["max_wait_ms"],
        params["max_signal_quote_age_ms"],
        params["one_way_taker_fee_bps"],
        params["extra_slippage_bps"],
        params["top_level_capacity_fraction"],
    )
    expected = unique_json(REPORT.read_bytes())
    actual = inspect(
        SOURCE,
        unique_json(INTENTS.read_bytes()),
        assumptions,
        MarkoutPolicy(
            tuple(params["horizons_ms"]), params["first_observation_tolerance_ms"]
        ),
    )
    if actual != expected:
        raise FrozenMarkoutError("Future-only markout reproduction mismatch")
    current_partition = partition(actual)
    if current_partition != unique_json(PARTITION.read_bytes()):
        raise FrozenMarkoutError("Separately frozen as-of/label partition mismatch")
    if labels_visible_asof(current_partition, 1013) != []:
        raise FrozenMarkoutError("Future label leaked into an as-of quote")
    if (
        len(current_partition["asof_quote_observations"]) != 7
        or len(current_partition["future_only_labels"]) != 21
    ):
        raise FrozenMarkoutError("Lost or duplicated labels in strict separation")
    # A genuinely separate software implementation processes the SAME raw source.
    # This demonstrates parser/markout agreement, never independent venue truth.
    from independent_v18_markout_replay import verify as verify_independent

    independent = verify_independent()
    if (
        independent["verified"] is not True
        or independent["messages"] != 152069
        or independent["historical_future_label_count"] != 21
        or independent["independent_market_source_verified"] is not False
    ):
        raise FrozenMarkoutError("Independent second software replay did not agree")
    sequences = tuple(x["quote_sequence"] for x in actual["results"])
    labels = tuple(
        tuple(y.get("label_observed_at_sequence") for y in x["labels"])
        for x in actual["results"]
    )
    if (
        actual["source_manifest_sha256"] != SOURCE_MANIFEST_SHA
        or actual["source_messages_audited"] != 152069
        or sequences != EXPECTED_QUOTE_SEQUENCES
        or labels != EXPECTED_LABEL_SEQUENCES
        or any(
            y["status"] != "HISTORICAL_FUTURE_MIDPRICE_LABEL_ONLY"
            for x in actual["results"]
            for y in x["labels"]
        )
        or any(
            y["label_available_at_received_utc"] is None
            for x in actual["results"]
            for y in x["labels"]
        )
        or actual["future_labels_are_never_input_features"] is not True
        or actual["actual_order_fills_verified"] is not False
        or actual["strategy_alpha_or_profitability_verified"] is not False
        or actual["orders_enabled"] is not False
    ):
        raise FrozenMarkoutError(
            "Pinned source label clock / no-fill semantics changed"
        )
    return {
        "verified": True,
        "status": "V18_ONE_HOUR_FUTURE_LABEL_REPLAYED_NOT_TRADING_ALPHA",
        "source_manifest_sha256": SOURCE_MANIFEST_SHA,
        "report_sha256": REPORT_SHA,
        "separate_feature_label_partition_sha256": PARTITION_SHA,
        "future_label_asof_join_guard_checked": True,
        "second_independent_software_replay_same_source_passed": True,
        "independently_sourced_venue_truth_verified": False,
        "predeclared_horizons_ms": [1000, 5000, 30000],
        "frozen_intents": len(sequences),
        "historical_label_observations": sum(map(len, labels)),
        "receipt_clock_not_exchange_clock": True,
        "future_outcomes_never_allowed_as_asof_features": True,
        "actual_order_fills_verified": False,
        "alpha_verified": False,
        "orders_enabled": False,
    }


def main():
    print(json.dumps(verify(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
