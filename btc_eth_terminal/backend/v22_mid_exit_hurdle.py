"""Read-only v22 research diagnostic, NOT a trading PnL or fill backtest.

Join pre-existing frozen v17 static L2 execution *references*, v18 future-only
midprice labels and v20 canonical maker conditional tape scenarios. Explicit
fee/slippage scenarios are loaded and validated before market evidence.
Only historical 7-anchor descriptive engineering fixtures are admitted.
No broker APIs, signals, execution, data-driven parameter selection or OOS.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

FROZEN_IDS = (
    "btc_buy_seq_1000",
    "eth_sell_seq_20000",
    "btc_depth_stress_30000",
    "btc_sell_seq_60000",
    "eth_depth_stress_80000",
    "eth_buy_seq_100000",
    "btc_buy_seq_150000",
)
HORIZONS = (1000, 5000, 30000)
WINDOWS = ("5000", "30000")
BPS = Decimal(10000)


class EvidenceReject(ValueError):
    pass


def load(path: Path) -> dict:
    """Strict bounded JSON input, immutable for the duration of one load."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise EvidenceReject("Invalid or oversized evidence file")
    raw = path.read_bytes()
    if len(raw) > 1_000_000:
        raise EvidenceReject("Oversized evidence input")

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise EvidenceReject("Duplicate evidence JSON key")
            out[k] = v
        return out

    def constant(t):
        raise EvidenceReject(f"Non-finite JSON numeric token: {t}")

    try:
        obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EvidenceReject("Unparseable evidence JSON") from exc
    if not isinstance(obj, dict):
        raise EvidenceReject("Evidence root must be object")
    return obj


def dec(value, label, *, positive=False, allow_negative=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 60:
        raise EvidenceReject(f"{label} must be a bounded decimal STRING")
    try:
        x = Decimal(value)
    except InvalidOperation as exc:
        raise EvidenceReject(f"Invalid {label}") from exc
    if not x.is_finite() or x.adjusted() > 20 or x.as_tuple().exponent < -30:
        raise EvidenceReject(f"{label} not finite/bounded")
    if positive and x <= 0:
        raise EvidenceReject(f"{label} must be positive")
    if not allow_negative and x < 0:
        raise EvidenceReject(f"{label} must be nonnegative")
    return x


def exact_rows(document, key, *, ids=FROZEN_IDS):
    rows = document.get(key)
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise EvidenceReject("Missing/fewer/additional frozen observation anchors")
    indexed = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not str
            or row["id"] in indexed
        ):
            raise EvidenceReject("Duplicated or malformed anchor")
        indexed[row["id"]] = row
    if set(indexed) != set(ids):
        raise EvidenceReject("Frozen anchor identity mismatch")
    return indexed


def policy_checked(policy):
    if (
        set(policy) != {"schema", "source_cohort_role", "exit_scenarios"}
        or policy["schema"] != "v22_fixed_mid_exit_hurdles_v1"
        or policy["source_cohort_role"] != "DESCRIPTIVE_ENGINEERING_FIXTURE_NOT_OOS"
    ):
        raise EvidenceReject("Policy schema/cohort role not allowed")
    scenarios = policy["exit_scenarios"]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
        raise EvidenceReject("1..3 predeclared scenarios required")
    seen = set()
    valid = []
    for s in scenarios:
        if not isinstance(s, dict) or set(s) != {
            "id",
            "exit_fee_bps",
            "exit_slippage_bps",
        }:
            raise EvidenceReject("Unapproved policy fields")
        label = s["id"]
        if (
            not isinstance(label, str)
            or label
            not in ("baseline_mid_exit", "stress_mid_exit", "zero_cost_control")
            or label in seen
        ):
            raise EvidenceReject("Duplicate or unapproved scenario")
        seen.add(label)
        fee = dec(s["exit_fee_bps"], "exit_fee_bps")
        slip = dec(s["exit_slippage_bps"], "exit_slippage_bps")
        if fee > 100 or slip > 500:
            raise EvidenceReject("Unbounded hypothetical exit costs")
        valid.append((label, fee, slip))
    return valid


def evaluate(
    v17, v18, maker, policy, *, source_manifest_digest, input_shas, v19_a=None
):
    scenarios = policy_checked(policy)
    required_source = v17.get("source_manifest_sha256")
    if (
        not isinstance(required_source, str)
        or len(required_source) != 64
        or required_source != v18.get("source_manifest_sha256")
        or required_source != maker.get("source_manifest_sha256")
        or required_source != source_manifest_digest
    ):
        raise EvidenceReject(
            "Three evidence reports must bind to same audited source manifest"
        )
    if (
        v17.get("status") != "CONDITIONAL_STATIC_L2_DEPTH_DIAGNOSTIC_ONLY"
        or v18.get("status") != "READ_ONLY_FUTURE_MIDPRICE_MARKOUT_LABELS_NO_FILLS"
        or maker.get("status")
        != "FROZEN_CANONICAL_MAKER_SIDE_TAPE_SENSITIVITY_NOT_FILLS"
        or maker.get("sample_used_for_alpha_model_selection") is not False
        or maker.get("real_maker_fill_probability_verified") is not False
        or v18.get("future_labels_are_never_input_features") is not True
        or v18.get("orders_enabled") is not False
    ):
        raise EvidenceReject("Untrusted report type or prohibited execution claim")
    if tuple(v18.get("horizons_ms_predeclared", ())) != HORIZONS or tuple(
        maker.get("windows_frozen_ms", ())
    ) != (5000, 30000):
        raise EvidenceReject("Frozen horizon/window drift")
    if (
        dec(v17.get("fee_bps_hypothetical"), "entry fee") != 10
        or dec(v17.get("slippage_bps_hypothetical"), "entry slippage") != 5
    ):
        raise EvidenceReject("Frozen original entry cost assumptions drift")
    a, b, m = (
        exact_rows(doc, key)
        for doc, key in ((v17, "results"), (v18, "results"), (maker, "records"))
    )
    v19_anchors = exact_rows(v19_a, "results") if v19_a is not None else None
    if v19_anchors is not None and (
        v19_a.get("source_manifest_sha256") != required_source
        or v19_a.get("status") != "MAKER_PUBLIC_TRADE_FLOW_SCENARIOS_NOT_FILLS"
    ):
        raise EvidenceReject("v19-A frozen quote input mismatch")
    out = []
    for id_ in FROZEN_IDS:
        entry, marks, maker_row = a[id_], b[id_], m[id_]
        seq = entry.get("quote_sequence")
        product, side = entry.get("product_id"), entry.get("side")
        if (
            not isinstance(seq, int)
            or seq <= 0
            or seq != marks.get("quote_sequence")
            or seq != maker_row.get("frozen_source_anchor_sequence")
            or product not in ("BTC-USD", "ETH-USD")
            or side not in ("BUY", "SELL")
            or (product, side) != (marks.get("product_id"), marks.get("side"))
            or (product, side)
            != (
                maker_row.get("product_id"),
                maker_row.get("maker_side_at_passive_price"),
            )
            or marks.get("original_quote_status") != entry.get("original_quote_status")
        ):
            raise EvidenceReject(f"Cross-archive anchor mismatch: {id_}")
        if (
            marks.get("status") != "FUTURE_LABELS_SEPARATE_FROM_AS_OF_SIGNAL"
            or maker_row.get("user_order_submitted") is not False
            or maker_row.get("observed_exchange_ack") is not False
            or maker_row.get("certified_real_maker_fill_lower_bound_base") != "0"
        ):
            raise EvidenceReject("Promoted fill/label or invalid maker order claim")
        posted_price = dec(
            maker_row.get("passive_quote_price_usd"),
            "maker posted price",
            positive=True,
        )
        requested = dec(
            maker_row.get("requested_hypothetical_base"),
            "requested quantity",
            positive=True,
        )
        visible = dec(
            maker_row.get("initial_visible_same_side_level_base"),
            "visible ahead",
            positive=True,
        )
        sensitivity = {}
        for window in WINDOWS:
            scope = maker_row.get("time_windows_ms", {}).get(window)
            if (
                not isinstance(scope, dict)
                or scope.get("unverified_actual_fill_base") is not None
            ):
                raise EvidenceReject("Actual maker fill must remain unknown")
            flow = dec(
                scope.get("historical_maker_side_same_price_volume"),
                "public tape volume",
            )
            optimistic = dec(
                scope.get("conditional_zero_ahead_quantity_no_impact"), "zero ahead"
            )
            full_ahead = dec(
                scope.get(
                    "conditional_initial_displayed_ahead_fifo_no_cancel_quantity_no_impact"
                ),
                "full ahead",
            )
            if optimistic != min(requested, flow) or full_ahead != min(
                requested, max(Decimal(0), flow - visible)
            ):
                raise EvidenceReject("Maker queue arithmetic mismatch")
            sensitivity[window] = {
                "observed_matching_maker_side_volume_base": str(flow),
                "conditional_zero_ahead_base": str(optimistic),
                "conditional_displayed_ahead_no_cancels_base": str(full_ahead),
                "verified_actual_fill_base": None,
            }
        labels = marks.get("labels")
        if not isinstance(labels, list) or len(labels) != len(HORIZONS):
            raise EvidenceReject("Missing or changed frozen future horizon")
        quote_mid = dec(marks.get("quote_midprice"), "quote mid", positive=True)
        quote_ns = int(marks.get("quote_local_monotonic_ns", "-1"))
        if quote_ns < 0:
            raise EvidenceReject("Invalid observed quote clock")
        entry_ok = entry.get("status") == "STATIC_L2_DEPTH_REFERENCE_NOT_FILL"
        if (
            not entry_ok
            and entry.get("status")
            != "REJECTED_STATIC_DEPTH_NOT_DEMONSTRATED_WITHIN_LEVEL_LIMIT"
        ):
            raise EvidenceReject("Unknown static-depth outcome")
        ref_price = None
        if entry_ok:
            ref_price = dec(
                entry.get("static_vwap_with_hypothetical_slippage"),
                "static entry reference",
                positive=True,
            )
            dec(entry.get("static_vwap"), "entry VWAP reference", positive=True)
        last_label_seq = seq
        quote_recv = None
        if v19_anchors is not None:
            anchor = v19_anchors[id_]
            if (
                anchor.get("anchor_sequence") != seq
                or anchor.get("posted_price_hypothetical")
                != maker_row.get("passive_quote_price_usd")
                or anchor.get("product_id") != product
                or anchor.get("side") != side
            ):
                raise EvidenceReject("v19-A quote anchor mismatch")
            try:
                stamp = anchor["anchor_received_utc"]
                if not isinstance(stamp, str) or not stamp.endswith("Z"):
                    raise ValueError("Not explicit UTC")
                quote_recv = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (ValueError, TypeError, KeyError) as exc:
                raise EvidenceReject("Invalid frozen quote receive timestamp") from exc
        horizon_rows = []
        for label, expected_horizon in zip(labels, HORIZONS, strict=False):
            if (
                label.get("horizon_ms") != expected_horizon
                or label.get("label_not_available_to_signal") is not True
                or label.get("status") != "HISTORICAL_FUTURE_MIDPRICE_LABEL_ONLY"
            ):
                raise EvidenceReject("Future label visible early or horizon drift")
            lab_seq = label.get("label_observed_at_sequence")
            if type(lab_seq) is not int or lab_seq <= last_label_seq:
                raise EvidenceReject("Noncausal or repeated label source sequence")
            last_label_seq = lab_seq
            if quote_recv is not None:
                try:
                    stamp = label["label_available_at_received_utc"]
                    if not isinstance(stamp, str) or not stamp.endswith("Z"):
                        raise ValueError("Not explicit UTC")
                    label_recv = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if label_recv <= quote_recv:
                        raise ValueError("Label must arrive after quote")
                except (ValueError, TypeError, KeyError) as exc:
                    raise EvidenceReject(
                        "Future label receive clock precedes quote"
                    ) from exc
            delay = dec(label.get("observed_delay_ms"), "observed future label delay")
            tolerance = int(v18.get("first_observation_tolerance_ms_predeclared", 0))
            if (
                type(tolerance) is not int
                or not 0 <= tolerance <= 10_000
                or not Decimal(expected_horizon)
                <= delay
                <= Decimal(expected_horizon + tolerance)
            ):
                raise EvidenceReject(
                    "Future label not first timely observation after horizon"
                )
            future = dec(label.get("future_midprice"), "future mid", positive=True)
            expected_move = (
                ((future / quote_mid) - 1) * BPS * (1 if side == "BUY" else -1)
            )
            old = dec(
                label.get("side_signed_midprice_move_bps"),
                "archived markout",
                allow_negative=True,
            )
            if abs(expected_move - old) > Decimal("1e-20"):
                raise EvidenceReject(
                    "Archived signed midprice label inconsistent with raw prices"
                )
            diagnostics = {}
            for s_id, exit_fee, exit_slip in scenarios:
                if ref_price is None:
                    diagnostics[s_id] = None
                    continue
                # Entry reference already includes hypothetical 5bps slippage.
                # Buy: hypothetically sell at F*(1-s), sell-fee on that notional.
                # Sell: hypothetically cover at F*(1+s), buy-fee on that notional.
                if side == "BUY":
                    exit_cash = future * (1 - exit_slip / BPS) * (1 - exit_fee / BPS)
                    start_cash = ref_price * (1 + Decimal(10) / BPS)
                    theoretical_edge = (exit_cash - start_cash) / ref_price * BPS
                else:
                    exit_cash = future * (1 + exit_slip / BPS) * (1 + exit_fee / BPS)
                    start_cash = ref_price * (1 - Decimal(10) / BPS)
                    theoretical_edge = (start_cash - exit_cash) / ref_price * BPS
                diagnostics[s_id] = str(theoretical_edge)
            horizon_rows.append(
                {
                    "horizon_ms": expected_horizon,
                    "future_label_sequence": lab_seq,
                    "future_label_observed_delay_ms": str(delay),
                    "future_only_midprice": str(future),
                    "signed_quote_mid_markout_bps": str(expected_move),
                    "hypothetical_mid_exit_cash_edge_bps_not_pnl": diagnostics,
                    "strategy_decision_may_read_future_label": False,
                }
            )
        out.append(
            {
                "id": id_,
                "product_id": product,
                "side": side,
                "first_eligible_quote_sequence": seq,
                "quote_midprice": str(quote_mid),
                "hypothetical_passive_maker_price": str(posted_price),
                "hypothetical_entry_reference_with_5bp_slippage": str(ref_price)
                if ref_price is not None
                else None,
                "static_taker_depth_status": entry["status"],
                "original_best_level_quote_status": entry["original_quote_status"],
                "hypothetical_sell_requires_independent_inventory_or_short_eligibility": side
                == "SELL",
                "maker_queue_scenarios_not_fills": sensitivity,
                "future_only_labels": horizon_rows,
                "verified_real_maker_or_taker_fills": None,
            }
        )
    return {
        "status": "V22_FROZEN_HISTORICAL_MID_EXIT_HURDLE_DIAGNOSTIC_NOT_EXECUTABLE_PNL",
        "source_manifest_sha256": required_source,
        "input_report_sha256": input_shas,
        "cohort_role": "DESCRIPTIVE_ENGINEERING_FIXTURE_NOT_OOS",
        "frozen_observations": len(out),
        "frozen_horizons_ms": list(HORIZONS),
        "cost_scenarios_predeclared": [
            {"id": x, "exit_fee_bps": str(f), "exit_slippage_bps": str(s)}
            for x, f, s in scenarios
        ],
        "entry_fee_bps_hypothetical_unverified": "10",
        "entry_slippage_bps_hypothetical_unverified": "5",
        "mid_exit_is_NOT_executable_quote_or_fill": True,
        "entry_quote_is_NOT_fill": True,
        "user_order_ack_latency_or_queue_verified": False,
        "account_specific_fees_verified": False,
        "actual_fill_or_realized_pnl_verified": False,
        "directional_sell_short_eligibility_verified": False,
        "alpha_or_out_of_sample_evidence_verified": False,
        "orders_or_credentials_present": False,
        "no_model_parameter_selection_performed": True,
        "results": out,
    }


def produce(root: Path, *, output: Path):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceReject("Unsafe source root")
    policy_path = root / "examples/v22_fixed_exit_scenarios.json"
    policy = load(policy_path)
    policy_checked(policy)  # validate policy BEFORE reading source or outcome labels
    paths = {
        "v17_static_depth": root / "P1_STATIC_DEPTH_STRESS_v17.json",
        "v18_future_labels": root / "P1_POST_QUOTE_MARKOUT_v18.json",
        "v20_maker_scenarios": root / "V20_CANONICAL_MAKER_SCENARIOS.json",
        "v22_policy": policy_path,
        "v19_a_anchor_archive": root / "P1_MAKER_QUEUE_SCENARIOS_v19.json",
    }
    # Check read-only frozen evidence hashes before accessing future-only labels.
    lock = load(root / "examples/v22_frozen_evidence_lock.json")
    if lock.get("schema") != "v22_frozen_engineering_evidence_lock_v1" or set(
        lock.get("files", {})
    ) != {
        "P1_STATIC_DEPTH_STRESS_v17.json",
        "P1_POST_QUOTE_MARKOUT_v18.json",
        "V20_CANONICAL_MAKER_SCENARIOS.json",
        "P1_MAKER_QUEUE_SCENARIOS_v19.json",
    }:
        raise EvidenceReject("Missing frozen evidence lock")
    for fname, expected in lock["files"].items():
        path = root / fname
        if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != expected:
            raise EvidenceReject("Frozen source-derived analysis tampered: " + fname)
    objects = {k: load(path) for k, path in paths.items()}
    source = root / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH"
    manifest = source / "manifest.json"
    if source.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise EvidenceReject("Original source manifest unavailable or unsafe")
    # Public source admission is independently performed by prior pinned audit_soak.
    from audit_soak import audit

    adm = audit(source)
    if (
        adm.get("eligible_for_point_in_time_feature_research") is not True
        or adm.get("connection_count") != 1
        or adm.get("message_count") != 152069
    ):
        raise EvidenceReject("Historical market source rejected or truncated")
    digest = sha256(manifest.read_bytes()).hexdigest()
    if digest != lock.get("source_manifest_sha256"):
        raise EvidenceReject("Raw source manifest differs from frozen cohort")
    result = evaluate(
        objects["v17_static_depth"],
        objects["v18_future_labels"],
        objects["v20_maker_scenarios"],
        policy,
        source_manifest_digest=digest,
        input_shas={k: sha256(p.read_bytes()).hexdigest() for k, p in paths.items()},
        v19_a=objects["v19_a_anchor_archive"],
    )
    output = Path(output)
    if output.is_symlink() or output.resolve().is_relative_to(source.resolve()):
        raise EvidenceReject("Unsafe diagnostic output path")
    tmp = output.with_suffix(output.suffix + ".partial")
    if tmp.is_symlink() or tmp.exists():
        raise EvidenceReject("Refuse to overwrite existing partial result")
    raw = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    with tmp.open("xb") as fh:
        fh.write(raw)
        fh.flush()
        import os

        os.fsync(fh.fileno())
    os.replace(tmp, output)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    out = produce(args.root, output=args.output)
    print(
        json.dumps(
            {
                "status": out["status"],
                "frozen_observations": out["frozen_observations"],
                "horizons": out["frozen_horizons_ms"],
            }
        )
    )
