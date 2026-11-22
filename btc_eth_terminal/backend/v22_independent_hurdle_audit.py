"""Independent arithmetic & evidence consistency audit of v22 fixed report.

Separate formula, no import of v22_mid_exit_hurdle. Does NOT certify trading
fills or independently attest Coinbase exchange truth.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

IDS = (
    "btc_buy_seq_1000",
    "eth_sell_seq_20000",
    "btc_depth_stress_30000",
    "btc_sell_seq_60000",
    "eth_depth_stress_80000",
    "eth_buy_seq_100000",
    "btc_buy_seq_150000",
)
HORIZONS = (1000, 5000, 30000)
B = Decimal("10000")


class IndependentMismatch(ValueError):
    pass


def verify(root: Path, report: dict) -> dict:
    root = Path(root)
    if (
        report.get("status")
        != "V22_FROZEN_HISTORICAL_MID_EXIT_HURDLE_DIAGNOSTIC_NOT_EXECUTABLE_PNL"
    ):
        raise IndependentMismatch("Unexpected report type")
    if (
        report.get("alpha_or_out_of_sample_evidence_verified") is not False
        or report.get("actual_fill_or_realized_pnl_verified") is not False
    ):
        raise IndependentMismatch("Unverified claim promoted")
    loads = lambda x: json.loads((root / x).read_text())
    depth = loads("P1_STATIC_DEPTH_STRESS_v17.json")
    future = loads("P1_POST_QUOTE_MARKOUT_v18.json")
    maker = loads("V20_CANONICAL_MAKER_SCENARIOS.json")
    policy = loads("examples/v22_fixed_exit_scenarios.json")
    anch = loads("P1_MAKER_QUEUE_SCENARIOS_v19.json")
    srcbytes = (
        root
        / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH/manifest.json"
    ).read_bytes()
    manifest_sha = sha256(srcbytes).hexdigest()
    if report.get("source_manifest_sha256") != manifest_sha or any(
        x.get("source_manifest_sha256") != manifest_sha
        for x in (depth, future, maker, anch)
    ):
        raise IndependentMismatch("Source manifest not the same")
    srcs = {
        x: {q["id"]: q for q in doc[key]}
        for x, doc, key in (
            ("entry", depth, "results"),
            ("label", future, "results"),
            ("maker", maker, "records"),
            ("anchor", anch, "results"),
        )
    }
    if any(set(x) != set(IDS) for x in srcs.values()):
        raise IndependentMismatch("Changed frozen IDs")
    seen = report.get("results")
    if not isinstance(seen, list) or tuple(x.get("id") for x in seen) != IDS:
        raise IndependentMismatch("Report rows not fixed")
    if len(report.get("cost_scenarios_predeclared", [])) != len(
        policy["exit_scenarios"]
    ):
        raise IndependentMismatch("Lost cost scenario")
    compared = 0
    for row in seen:
        key = row["id"]
        e = srcs["entry"][key]
        f = srcs["label"][key]
        m = srcs["maker"][key]
        a = srcs["anchor"][key]
        if (
            row["first_eligible_quote_sequence"] != f["quote_sequence"]
            or row["first_eligible_quote_sequence"] != e["quote_sequence"]
            or row["first_eligible_quote_sequence"]
            != m["frozen_source_anchor_sequence"]
            or row["first_eligible_quote_sequence"] != a["anchor_sequence"]
        ):
            raise IndependentMismatch("Quote anchor source sequence differs")
        if row["hypothetical_passive_maker_price"] != m["passive_quote_price_usd"]:
            raise IndependentMismatch("Maker quote price differs")
        if row.get("verified_real_maker_or_taker_fills") is not None:
            raise IndependentMismatch("Real fills illegally asserted")
        qty = Decimal(m["requested_hypothetical_base"])
        ahead = Decimal(m["initial_visible_same_side_level_base"])
        for window, values in row["maker_queue_scenarios_not_fills"].items():
            base = m["time_windows_ms"][window]
            trade = Decimal(base["historical_maker_side_same_price_volume"])
            opt = min(qty, trade)
            pessim = min(qty, max(Decimal(0), trade - ahead))
            if (
                Decimal(values["conditional_zero_ahead_base"]) != opt
                or Decimal(values["conditional_displayed_ahead_no_cancels_base"])
                != pessim
                or values["verified_actual_fill_base"] is not None
            ):
                raise IndependentMismatch("Maker queue not independently reproducible")
        ref = (
            Decimal(e["static_vwap_with_hypothetical_slippage"])
            if e["status"] == "STATIC_L2_DEPTH_REFERENCE_NOT_FILL"
            else None
        )
        if (row["hypothetical_entry_reference_with_5bp_slippage"] is None) != (
            ref is None
        ):
            raise IndependentMismatch("Rejected static depth was not preserved")
        label_rows = row["future_only_labels"]
        if len(label_rows) != 3:
            raise IndependentMismatch("Missing future label")
        for pos, (obs, original) in enumerate(zip(label_rows, f["labels"])):
            horizon = HORIZONS[pos]
            if (
                obs["horizon_ms"] != horizon
                or obs["future_label_sequence"]
                != original["label_observed_at_sequence"]
                or obs.get("strategy_decision_may_read_future_label") is not False
            ):
                raise IndependentMismatch("Noncausal horizon or future-label join")
            price = Decimal(original["future_midprice"])
            quote = Decimal(f["quote_midprice"])
            sign = Decimal(1 if e["side"] == "BUY" else -1)
            signed = (price - quote) / quote * B * sign
            if abs(Decimal(obs["signed_quote_mid_markout_bps"]) - signed) > Decimal(
                "1e-20"
            ):
                raise IndependentMismatch("Signed midprice markout arithmetic")
            for spec in policy["exit_scenarios"]:
                name = spec["id"]
                fee = Decimal(spec["exit_fee_bps"]) / B
                slippage = Decimal(spec["exit_slippage_bps"]) / B
                got = obs["hypothetical_mid_exit_cash_edge_bps_not_pnl"][name]
                if ref is None:
                    if got is not None:
                        raise IndependentMismatch("Rejected quote generated economics")
                else:
                    if e["side"] == "BUY":
                        # Distinct ratio form, checking the original producer's cash
                        # difference.
                        want = B * (
                            price / ref * (1 - fee) * (1 - slippage) - Decimal("1.001")
                        )
                    else:
                        want = B * (
                            Decimal("0.999") - price / ref * (1 + fee) * (1 + slippage)
                        )
                    if got is None or abs(Decimal(got) - want) > Decimal("1e-20"):
                        raise IndependentMismatch(
                            "Exit fee/slippage arithmetic diverged"
                        )
                compared += 1
    return {
        "status": "V22_INDEPENDENT_ARITHMETIC_AGREEMENT_NOT_MARKET_TRUTH",
        "source_manifest_sha256": manifest_sha,
        "source_is_same_provider_archive": True,
        "frozen_observations": len(IDS),
        "horizons_compared": len(IDS) * len(HORIZONS),
        "scenario_comparisons": compared,
        "real_fill_or_alpha_validated": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    x = verify(a.root, json.loads(a.report.read_text()))
    if a.output.exists():
        raise IndependentMismatch("Refuse overwrite independent audit result")
    a.output.write_text(json.dumps(x, indent=2, sort_keys=True) + "\n")
    print(json.dumps(x, sort_keys=True))


if __name__ == "__main__":
    main()
