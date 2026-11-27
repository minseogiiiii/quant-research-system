"""Offline registered F6 trial runner on whole, externally pinned native epochs.

Session returns reset capital, remain segmented, and cannot be passed as a
continuous regular grid to H5. No holdout files accepted. No live dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

from f6_liquidity_recovery import POLICY
from f7_pnl_attribution import summarize
from h1_f6_dataset_replay import replay_dataset
from h4_cross_session_catalog import catalog
from h5_experiment_registry import append, create, need, series_hash, sha, snapshot
from independent_soak_replay import strict_json

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "backend/f6_liquidity_recovery.py",
    "backend/f7_pnl_attribution.py",
    "backend/f5_execution_depth.py",
    "backend/post_quote_markout.py",
    "backend/f4_depth_buckets.py",
    "scripts/h1_f6_dataset_replay.py",
    "scripts/h6_registered_strategy_runner.py",
)
FAMILY = "F6_RECOVERY_WHOLE_SESSION_V1"
MAX_SESSIONS = 16
MAX_TRIALS = 8


def code_pin():
    return sha(
        {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}
    )


def policy_for(params):
    need(
        type(params) is dict
        and set(params) == {"product", "hold_ms"}
        and params["product"] in ("BTC-USD", "ETH-USD")
        and type(params["hold_ms"]) is int
        and params["hold_ms"] in (5000, 10000, 30000, 60000),
        "H6_PARAMS",
    )
    return dict(POLICY, hold_ms=params["hold_ms"])


def prepare(entries, embargo_ms, trials, seed, *, fixture=False):
    need(
        1 <= len(entries) <= MAX_SESSIONS
        and all(e["role"] == "DEVELOPMENT" for e in entries),
        "H6_DEVELOPMENT_ONLY",
    )
    need(type(trials) is list and 1 <= len(trials) <= MAX_TRIALS, "H6_TRIAL_BUDGET")
    need(
        all(policy_for(t) for t in trials) and len({t["product"] for t in trials}) == 1,
        "H6_PRODUCT",
    )
    cat = catalog(entries, embargo_ms, fixture=fixture)
    split = {
        "scope": "WHOLE_EPOCH_DEVELOPMENT_ONLY",
        "embargo_ms": embargo_ms,
        "capital_reset_per_epoch": True,
        "unresolved_or_incomplete_grid": "FAIL_TRIAL",
    }
    plan = {
        "schema": "h5_experiment_plan_v1",
        "source_kind": "SYNTHETIC_TEST_ONLY" if fixture else "DEVELOPMENT",
        "dataset_catalog_sha256": cat["catalog_sha256"],
        "strategy_code_sha256": code_pin(),
        "cost_policy_sha256": sha(POLICY),
        "split_policy_sha256": sha(split),
        "family": FAMILY,
        "seed": seed,
        "trials": [{"trial_id": sha(t), "parameters": t} for t in trials],
    }
    return plan, cat


def run_session(entry, row, params, *, fixture=False):
    policy = policy_for(params)

    def projector(packets, product):
        return summarize(packets, product, policy)

    r, a = replay_dataset(
        entry["dataset_path"],
        row["source_manifest_sha256"],
        params["product"],
        fixture=fixture,
        projector=projector,
        receipt_pin=row["dataset_receipt_sha256"],
    )
    need(
        r["source_messages"] == row["source_messages"]
        and r["source_first_ms"] == row["first_receiver_ms"]
        and r["source_last_ms"] == row["last_receiver_ms"],
        "H6_SOURCE_PARITY",
    )
    need(
        a["complete_regular_grid"]
        and a["final_flat_cash_reconciled"]
        and not r["unresolved_position"]
        and len(a["returns"]) >= 2,
        "H6_INCOMPLETE_SESSION",
    )
    net = float(Decimal(r["final_cash_if_flat"]) / Decimal(r["baseline_cash"]) - 1)
    compounded = math.prod(1 + x for x in a["returns"]) - 1
    need(math.isclose(net, compounded, rel_tol=1e-8, abs_tol=1e-10), "H6_RETURN_PARITY")
    return {
        "source_manifest_sha256": row["source_manifest_sha256"],
        "dataset_receipt_sha256": row["dataset_receipt_sha256"],
        "net_return": net,
        "closed_roundtrips": r["closed_roundtrips"],
        "valid_decisions": r["valid_decisions"],
        "missing_decisions": r["missing_decisions"],
        "return_series_sha256": series_hash(
            a["returns"], a["timestamps"], a["interval_ms"]
        ),
        "strategy_report_sha256": sha(r),
        "attribution_sha256": sha(a),
        "initial_cash_reset": True,
    }


def run(entries, embargo_ms, trials, seed, journal, *, fixture=False):
    plan, cat = prepare(entries, embargo_ms, trials, seed, fixture=fixture)
    pin, head = create(journal, plan)
    by_pin = {e["dataset_receipt_sha256"]: e for e in entries}
    results = []
    for trial in plan["trials"]:
        tid = trial["trial_id"]
        head = append(journal, pin, head, {"event": "START", "trial_id": tid})
        sessions = []
        try:
            for row in cat["sessions"]:
                sessions.append(
                    run_session(
                        by_pin[row["dataset_receipt_sha256"]],
                        row,
                        trial["parameters"],
                        fixture=fixture,
                    )
                )
            result = {
                "trial_id": tid,
                "status": "COMPLETE",
                "sessions": sessions,
                "mean_session_net_return": sum(s["net_return"] for s in sessions)
                / len(sessions),
            }
        except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as exc:
            # Stable reason, no raw payload or path disclosure. Partial session
            # results remain visible, never silently become a complete trial.
            reason = (
                "INCOMPLETE_SESSION"
                if str(exc).endswith("H6_INCOMPLETE_SESSION")
                else "REPLAY_OR_ACCOUNTING_REJECTED"
            )
            head = append(
                journal, pin, head, {"event": "FAIL", "trial_id": tid, "reason": reason}
            )
            results.append(
                {
                    "trial_id": tid,
                    "status": "FAILED",
                    "reason": reason,
                    "sessions": sessions,
                }
            )
            continue
        segmented = sha(
            {
                "basis": "SEGMENTED_WHOLE_EPOCH_RETURNS_NOT_REGULAR_GRID",
                "sessions": [s["return_series_sha256"] for s in sessions],
            }
        )
        head = append(
            journal,
            pin,
            head,
            {
                "event": "COMPLETE",
                "trial_id": tid,
                "return_series_sha256": segmented,
                "result_sha256": sha(result),
            },
        )
        results.append(result)
    registry = snapshot(journal, pin, head)
    need(registry["all_terminal"], "H6_TERMINAL")
    return {
        "schema": "h6_registered_strategy_run_v1",
        "plan": plan,
        "plan_sha256": pin,
        "registry_head_sha256": head,
        "catalog": cat,
        "results": results,
        "all_complete": registry["all_complete"],
        "return_basis": "SEGMENTED_WHOLE_EPOCH_RETURNS_NOT_REGULAR_GRID",
        "holdout_read": False,
        "selection_performed": False,
        "actual_fills_verified": False,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("plan", type=Path)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    need(a.plan.stat().st_size <= 1000000 and not a.output.exists(), "H6_OUTPUT_PLAN")
    v = strict_json(a.plan.read_bytes())
    need(set(v) == {"sessions", "embargo_ms", "trials", "seed"}, "H6_INPUT")
    r = run(v["sessions"], v["embargo_ms"], v["trials"], v["seed"], a.journal)
    with a.output.open("x") as f:
        json.dump(r, f, sort_keys=True, allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
