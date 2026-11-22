"""Predeclared scenario sensitivity over read-only BTC/ETH indicative taker quotes.

Never mines strategies, selects a winning parameter, executes orders, or
interprets hypothetical cashflows as PnL. Each scenario must be explicitly
specified before reading the archive; a source is reaudited for every scenario.
"""

from __future__ import annotations

import argparse
import json
import re
from hashlib import sha256
from pathlib import Path

from taker_quote_envelope import (
    Assumptions,
    EnvelopeError,
    _intents,
    evaluate,
    unique_json,
)

SCENARIO_ID = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
REQUIRED_FIELDS = frozenset(
    (
        "id",
        "latency_ms",
        "max_wait_ms",
        "max_signal_quote_age_ms",
        "one_way_taker_fee_bps",
        "extra_slippage_bps",
        "top_level_capacity_fraction",
    )
)


class SensitivityError(ValueError):
    pass


def _validate_scenarios(scenarios):
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 6:
        raise SensitivityError("Require exactly 2..6 preregistered scenarios")
    used = set()
    out = []
    for item in scenarios:
        if not isinstance(item, dict) or set(item) != REQUIRED_FIELDS:
            raise SensitivityError("Unexpected or incomplete scenario fields")
        identifier = item["id"]
        if (
            not isinstance(identifier, str)
            or not SCENARIO_ID.fullmatch(identifier)
            or identifier in used
        ):
            raise SensitivityError("Invalid or duplicate scenario id")
        used.add(identifier)
        vals = Assumptions(
            item["latency_ms"],
            item["max_wait_ms"],
            item["max_signal_quote_age_ms"],
            item["one_way_taker_fee_bps"],
            item["extra_slippage_bps"],
            item["top_level_capacity_fraction"],
        )
        try:
            vals.validated()
        except EnvelopeError as exc:
            raise SensitivityError(
                "Scenario " + identifier + " has invalid economics"
            ) from exc
        out.append((identifier, vals))
    if scenarios[0]["id"] != "baseline":
        raise SensitivityError("First scenario must be baseline, no later selection")
    return out


def compare(source: Path, intents: list, scenarios: list) -> dict:
    configs = _validate_scenarios(
        scenarios
    )  # validate all BEFORE accessing market source
    try:
        _intents(intents)
    except EnvelopeError as exc:
        raise SensitivityError("Observation intents malformed") from exc
    intent_fingerprint = sha256(
        json.dumps(intents, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scenarios_fingerprint = sha256(
        json.dumps(scenarios, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    matrix = []
    baseline_keys = None
    source_manifest = None
    for label, assumptions in configs:
        result = evaluate(source, intents, assumptions)
        if source_manifest is None:
            source_manifest = result["source_manifest_sha256"]
        elif source_manifest != result["source_manifest_sha256"]:
            raise SensitivityError("Source manifest changed across scenario passes")
        keys = [r["id"] for r in result["results"]]
        if baseline_keys is not None and baseline_keys != keys:
            raise SensitivityError("Scenario intent accounting mismatch")
        baseline_keys = keys
        matrix.append(
            {
                "scenario": label,
                "assumed_latency_ms": assumptions.latency_ms,
                "assumed_fee_bps": assumptions.one_way_taker_fee_bps,
                "assumed_slippage_bps": assumptions.extra_slippage_bps,
                "assumed_capacity_fraction": assumptions.top_level_capacity_fraction,
                "observations": result["results"],
                "indicative_quote_count": sum(
                    r["status"] == "INDICATIVE_TAKER_QUOTE_ONLY"
                    for r in result["results"]
                ),
                "rejected_count": sum(
                    r["status"] != "INDICATIVE_TAKER_QUOTE_ONLY"
                    for r in result["results"]
                ),
            }
        )
    return {
        "status": "PREDECLARED_HYPOTHETICAL_COST_SENSITIVITY_NO_ORDERS",
        "source_manifest_sha256": source_manifest,
        "frozen_intent_declaration_sha256": intent_fingerprint,
        "frozen_scenario_declaration_sha256": scenarios_fingerprint,
        "scenario_count": len(matrix),
        "observation_count": len(intents),
        "scenario_matrix": matrix,
        "best_scenario_selected": False,
        "actual_order_sent": False,
        "actual_fills_verified": False,
        "actual_fee_latency_measured": False,
        "market_impact_or_maker_queue_verified": False,
        "profitability_or_alpha_verified": False,
        "note": "Scenario sensitivity is not an OOS model selection, execution fill or PnL estimate.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    intents = unique_json(args.intents.read_bytes())
    scenarios = unique_json(args.scenarios.read_bytes())
    report = compare(args.source, intents, scenarios)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(serialized)
    print(serialized)


if __name__ == "__main__":
    main()
