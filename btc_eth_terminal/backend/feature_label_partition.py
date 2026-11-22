"""Strict read-only point-in-time partition for archived quote and future labels.

Creates separate as-of observation records and future outcome labels. It must
never join a future label into a feature row at the earlier quote observation.
This is a source availability contract, NOT a model or execution backtest.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from taker_quote_envelope import number, unique_json


class FeatureLabelPartitionError(ValueError):
    pass


def _parse_utc(text):
    if not isinstance(text, str) or not text.endswith("Z"):
        raise FeatureLabelPartitionError("Missing ISO-8601 UTC source receipt")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeatureLabelPartitionError("Malformed UTC receipt") from exc
    if dt.utcoffset() != timedelta(0):
        raise FeatureLabelPartitionError("Non-UTC source receipt")
    return dt


def partition(markout: dict) -> dict:
    if (
        not isinstance(markout, dict)
        or markout.get("status") != "READ_ONLY_FUTURE_MIDPRICE_MARKOUT_LABELS_NO_FILLS"
        or markout.get("future_labels_are_never_input_features") is not True
        or markout.get("orders_enabled") is not False
        or markout.get("actual_order_fills_verified") is not False
    ):
        raise FeatureLabelPartitionError(
            "Input is not an approved read-only markout report"
        )
    observations = []
    labels = []
    seen = set()
    for item in markout["results"]:
        id_ = item["id"]
        if id_ in seen:
            raise FeatureLabelPartitionError("Duplicate observation ID")
        seen.add(id_)
        if item["status"] == "NO_FUTURE_LABEL_WITHOUT_VALID_QUOTE":
            if item["labels"] != []:
                raise FeatureLabelPartitionError("Rejected signal has future outcomes")
            continue
        if (
            set(item)
            != {
                "id",
                "product_id",
                "side",
                "signal_sequence",
                "original_quote_status",
                "quote_sequence",
                "quote_local_monotonic_ns",
                "quote_midprice",
                "status",
                "labels",
            }
            or item["status"] != "FUTURE_LABELS_SEPARATE_FROM_AS_OF_SIGNAL"
            or type(item["quote_sequence"]) is not int
            or type(item["signal_sequence"]) is not int
            or item["quote_sequence"] <= item["signal_sequence"]
            or not isinstance(item["quote_local_monotonic_ns"], str)
            or not item["quote_local_monotonic_ns"].isdecimal()
        ):
            raise FeatureLabelPartitionError("Unsafe or ambiguous as-of quote schema")
        number(item["quote_midprice"], "quote_midprice")
        observations.append(
            {
                "id": id_,
                "product_id": item["product_id"],
                "side": item["side"],
                "signal_sequence": item["signal_sequence"],
                "available_asof_sequence": item["quote_sequence"],
                "asof_midprice": item["quote_midprice"],
                "original_quote_status": item["original_quote_status"],
            }
        )
        seen_horizons = set()
        for label in item["labels"]:
            horizon = label["horizon_ms"]
            if type(horizon) is not int or horizon <= 0 or horizon in seen_horizons:
                raise FeatureLabelPartitionError("Duplicate or invalid outcome horizon")
            seen_horizons.add(horizon)
            if label["status"] != "HISTORICAL_FUTURE_MIDPRICE_LABEL_ONLY":
                if set(label) not in (
                    {"status", "horizon_ms"},
                    {
                        "status",
                        "horizon_ms",
                        "first_later_quote_sequence",
                        "observed_delay_ms",
                    },
                ):
                    raise FeatureLabelPartitionError(
                        "Invalid unavailable-label metadata"
                    )
                continue
            if (
                set(label)
                != {
                    "status",
                    "horizon_ms",
                    "label_observed_at_sequence",
                    "label_available_at_received_utc",
                    "observed_delay_ms",
                    "future_midprice",
                    "side_signed_midprice_move_bps",
                    "label_not_available_to_signal",
                }
                or label["label_not_available_to_signal"] is not True
                or type(label["label_observed_at_sequence"]) is not int
                or label["label_observed_at_sequence"] <= item["quote_sequence"]
            ):
                raise FeatureLabelPartitionError(
                    "Future label violates as-of availability"
                )
            _parse_utc(label["label_available_at_received_utc"])
            number(label["future_midprice"], "future_midprice")
            # Side-signed returns can be negative, unlike the decimal helper for sizes.
            signed = label["side_signed_midprice_move_bps"]
            if not isinstance(signed, str) or len(signed) > 100:
                raise FeatureLabelPartitionError("Malformed signed price movement")
            try:
                delta = Decimal(signed)
            except Exception as exc:
                raise FeatureLabelPartitionError("Malformed signed movement") from exc
            if not delta.is_finite() or abs(delta) > Decimal("100000"):
                raise FeatureLabelPartitionError(
                    "Outside bounded historical price movement"
                )
            labels.append(
                {
                    "id": id_,
                    "horizon_ms": horizon,
                    "available_after_source_sequence": label[
                        "label_observed_at_sequence"
                    ],
                    "available_after_received_utc": label[
                        "label_available_at_received_utc"
                    ],
                    "future_midprice": label["future_midprice"],
                    "side_signed_midprice_move_bps": signed,
                }
            )
    return {
        "status": "STRICT_FEATURE_LABEL_PARTITION_NOT_STRATEGY_DATA",
        "source_manifest_sha256": markout["source_manifest_sha256"],
        "asof_quote_observations": observations,
        "future_only_labels": labels,
        "feature_available_on_quote_only_not_signal": True,
        "no_future_label_in_asof_records": True,
        "historical_label_data_cannot_prove_alpha": True,
        "no_orders": True,
    }


def labels_visible_asof(result: dict, available_sequence: int) -> list:
    """Prevent accidental publication of future outcomes at earlier timestamps."""
    if (
        not isinstance(result, dict)
        or result.get("status") != "STRICT_FEATURE_LABEL_PARTITION_NOT_STRATEGY_DATA"
        or type(available_sequence) is not int
        or available_sequence < 0
    ):
        raise FeatureLabelPartitionError("Invalid as-of join request")
    if result.get("no_future_label_in_asof_records") is not True:
        raise FeatureLabelPartitionError("Untrusted partition")
    asof = {}
    for item in result["asof_quote_observations"]:
        if (
            set(item)
            != {
                "id",
                "product_id",
                "side",
                "signal_sequence",
                "available_asof_sequence",
                "asof_midprice",
                "original_quote_status",
            }
            or item["id"] in asof
            or type(item["available_asof_sequence"]) is not int
            or item["available_asof_sequence"] <= item["signal_sequence"]
        ):
            raise FeatureLabelPartitionError("Corrupt or leaked as-of record")
        asof[item["id"]] = item["available_asof_sequence"]
    visible = []
    for label in result["future_only_labels"]:
        if (
            set(label)
            != {
                "id",
                "horizon_ms",
                "available_after_source_sequence",
                "available_after_received_utc",
                "future_midprice",
                "side_signed_midprice_move_bps",
            }
            or label["id"] not in asof
            or type(label["available_after_source_sequence"]) is not int
            or label["available_after_source_sequence"] <= asof[label["id"]]
        ):
            raise FeatureLabelPartitionError("Future label or availability is corrupt")
        if label["available_after_source_sequence"] <= available_sequence:
            visible.append(label)
    return visible


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report", type=Path)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    data = partition(unique_json(a.report.read_bytes()))
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if a.output:
        a.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
