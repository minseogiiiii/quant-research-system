"""v20 canonical read-only maker tape sensitivity from independently reconciled v19.

One original source, seven frozen descriptive anchors, two frozen windows. Coinbase
market_trades.side is the resting MAKER side. The event-time-gated receiver-side
selection is deliberately conservative and sensitivity is exposed separately.
These are not real fills, unconditional bounds, fee-verified PnL or alpha.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path


def build(reconciled, report_a):
    if (
        reconciled.get("status") != "V19_A_B_DEFINITIONS_RECONCILED_ON_SAME_ORIGINAL"
        or reconciled["source_manifest_sha256"] != report_a["source_manifest_sha256"]
        or len(reconciled["rows"]) != 7
        or len(report_a["results"]) != 7
    ):
        raise ValueError("Cannot promote a partially reconciled lineage")
    anchors = {q["id"]: q for q in report_a["results"]}
    records = []
    for row in reconciled["rows"]:
        q = anchors[row["id"]]
        if (
            row["anchor_source_sequence"] != q["anchor_sequence"]
            or row["passive_side"] != q["side"]
            or row["anchor_price_usd"] != q["posted_price_hypothetical"]
        ):
            raise ValueError("Anchor divergence")
        requested = Decimal(q["requested_quantity"])
        ahead = Decimal(q["initial_visible_same_side_quantity"])
        if requested <= 0 or ahead <= 0:
            raise ValueError("Bad pinned hypothetical quantities")
        by_window = {}
        for window in (5000, 30000):
            cg = row["matrix"][f"{window}ms_maker_recv_and_event"]
            recv_only = row["matrix"][f"{window}ms_maker_recv"]
            cflow = Decimal(cg["volume"])
            received_flow = Decimal(recv_only["volume"])
            if cflow < 0 or received_flow < cflow:
                raise ValueError("Impossible event-time filter tally")
            by_window[str(window)] = {
                "historical_maker_side_same_price_volume": str(cflow),
                "receiver_only_trade_volume_sensitivity": str(received_flow),
                "excluded_pre_anchor_exchange_events_count": cg[
                    "pre_anchor_events_excluded"
                ],
                "conditional_zero_ahead_quantity_no_impact": str(min(requested, cflow)),
                (
                    'conditional_initial_displaye'
                    'd_ahead_fifo_no_cancel_quant'
                    'ity_no_impact'
                ): str(
                    min(requested, max(Decimal(0), cflow - ahead))
                ),
                "unverified_actual_fill_base": None,
            }
        records.append(
            {
                "id": row["id"],
                "venue": "Coinbase Advanced Trade public",
                "market_type": "spot",
                "product_id": row["product_id"],
                "maker_side_at_passive_price": row["passive_side"],
                "frozen_source_anchor_sequence": row["anchor_source_sequence"],
                "passive_quote_price_usd": row["anchor_price_usd"],
                "requested_hypothetical_base": str(requested),
                "initial_visible_same_side_level_base": str(ahead),
                "v19_b_upstream_taker_capacity_gate_included": row[
                    "v19_b_included_under_upstream_taker_depth_gate"
                ],
                "time_windows_ms": by_window,
                "certified_real_maker_fill_lower_bound_base": "0",
                "user_order_submitted": False,
                "observed_exchange_ack": False,
            }
        )
    return {
        "status": "FROZEN_CANONICAL_MAKER_SIDE_TAPE_SENSITIVITY_NOT_FILLS",
        "source_manifest_sha256": reconciled["source_manifest_sha256"],
        "sample_used_for_alpha_model_selection": False,
        "frozen_existing_candidate_ids": len(records),
        "baseline_assumed_local_quote_observation_delay_ms": 250,
        "canonical_trade_side": (
            'Coinbase Advanced market_tra'
            'des.side means MAKER / resti'
            'ng side'
        ),
        "trade_eligibility": (
            'post-anchor receiver order; '
            'exact passive quote price; m'
            'atching maker side; trade ev'
            'ent timestamp not earlier th'
            'an anchor receiver UTC'
        ),
        "warning": (
            'Receiver and exchange clocks'
            ' are uncalibrated, so cross-'
            'clock event-time gate is a s'
            'ensitivity choice, not proof'
            ' of order acceptance.'
        ),
        "windows_frozen_ms": [5000, 30000],
        "account_maker_fee_bps_verified": None,
        "real_maker_fill_probability_verified": False,
        "user_venue_order_acks_or_fills_verified": False,
        "conditional_amounts_are_not_unconditional_fill_bounds": True,
        "strategy_alpha_verified": False,
        "no_orders_sent": True,
        "records": records,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reconciliation", type=Path, required=True)
    p.add_argument("--original-a", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    obj = build(
        json.loads(args.reconciliation.read_text()),
        json.loads(args.original_a.read_text()),
    )
    args.output.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": obj["status"], "records": len(obj["records"])}))


if __name__ == "__main__":
    main()
