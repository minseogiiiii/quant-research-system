"""Metadata-only BTC/ETH source storage planning. Not a data-quality or latency audit.

Use after the independent source replay gates have already completed. Projected
rates are *hypothetical extrapolations from a bounded window*, never capacity
certifications or uptime claims. Does not connect to an exchange or broker.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

MIB = Decimal(1024**2)


def _strict_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("Duplicate JSON metadata key")
        out[key] = value
    return out


def project(
    archive: Path,
    *,
    target_seconds: int = 3600,
    burst_multiplier: Decimal = Decimal("3"),
) -> dict:
    if type(target_seconds) is not int or not 60 <= target_seconds <= 86400:
        raise ValueError("target_seconds must be between 60 and 86400")
    if (
        not isinstance(burst_multiplier, Decimal)
        or not burst_multiplier.is_finite()
        or not Decimal("1") <= burst_multiplier <= Decimal("10")
    ):
        raise ValueError("burst_multiplier must be finite from 1 through 10")
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError("Unsafe archive directory")
    mf = archive / "manifest.json"
    if mf.is_symlink() or not mf.is_file() or mf.stat().st_size > 500000:
        raise ValueError("Missing or oversized source manifest")
    meta = json.loads(mf.read_bytes(), object_pairs_hook=_strict_pairs)
    if (
        meta.get("format") != "btc_eth_raw_soak_v10"
        or meta.get("orders_enabled") is not False
    ):
        raise ValueError("Unsupported or unsafe source manifest")
    a = datetime.fromisoformat(meta["sampled_from_utc"].replace("Z", "+00:00"))
    b = datetime.fromisoformat(meta["finished_at_utc"].replace("Z", "+00:00"))
    if (
        not a.tzinfo
        or not b.tzinfo
        or a.utcoffset().total_seconds() != 0
        or b.utcoffset().total_seconds() != 0
    ):
        raise ValueError("Untrusted source clock format")
    elapsed = Decimal(str((b - a).total_seconds()))
    if elapsed < Decimal("10") or elapsed > Decimal("3800"):
        raise ValueError("Implausible collection interval")
    chunks = meta["chunks"]
    if not isinstance(chunks, list) or len(chunks) < 1 or len(chunks) > 10000:
        raise ValueError("Invalid chunk metadata")
    raw_bytes = meta["bytes"]
    saved_bytes = sum(c["archived_line_bytes"] for c in chunks)
    if (
        type(raw_bytes) is not int
        or raw_bytes < 0
        or saved_bytes <= 0
        or meta.get("messages", 0) <= 0
    ):
        raise ValueError("Invalid source volume metadata")
    producer = meta.get("resource_telemetry")
    if producer and producer.get("archived_jsonl_bytes") != saved_bytes:
        raise ValueError("Producer and manifest byte totals disagree")
    rate_raw = Decimal(raw_bytes) / elapsed
    rate_saved = Decimal(saved_bytes) / elapsed
    projected = int(
        (rate_saved * target_seconds).to_integral_value(rounding=ROUND_CEILING)
    )
    stress = int(
        (rate_saved * target_seconds * burst_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    actual_budget = (producer or {}).get("archive_budget_bytes")
    if actual_budget is not None and (
        type(actual_budget) is not int or actual_budget < saved_bytes
    ):
        raise ValueError("Configured archive limit smaller than archive")
    return {
        "status": "UNVERIFIED_CAPACITY_EXTRAPOLATION",
        "observed_window_seconds": str(elapsed),
        "observed_messages": meta["messages"],
        "observed_raw_bytes": raw_bytes,
        "observed_jsonl_bytes": saved_bytes,
        "observed_jsonl_bytes_per_second": round(float(rate_saved), 2),
        "observed_raw_bytes_per_second": round(float(rate_raw), 2),
        "target_duration_seconds": target_seconds,
        "projected_jsonl_bytes_at_observed_rate": projected,
        "stress_multiplier_assumption": str(burst_multiplier),
        "stress_projected_jsonl_bytes": stress,
        "stress_projected_jsonl_mib": round(float(Decimal(stress) / MIB), 2),
        "producer_configured_budget_bytes": actual_budget,
        "budget_exceeds_assumed_stress_projection": actual_budget >= stress
        if actual_budget is not None
        else None,
        "no_trading_orders": True,
        "source_replay_independently_verified_by_this_tool": False,
        "clock_offset_or_live_source_uptime_verified": False,
        "note": "Not a forecast: bursts, disk pressure, compressor ratios, crashes and provider outages are unknown.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--target-seconds", type=int, default=3600)
    p.add_argument("--burst-multiplier", type=Decimal, default=Decimal("3"))
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    value = project(
        a.archive, target_seconds=a.target_seconds, burst_multiplier=a.burst_multiplier
    )
    out = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if a.output:
        a.output.write_text(out)
    print(out)


if __name__ == "__main__":
    main()
