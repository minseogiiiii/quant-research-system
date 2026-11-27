"""Fail-closed research qualification of a pinned H6 development run.

No executable approval path: current H6/H7/H5 evidence does not establish
same-policy independent holdout performance, calibrated fills or live safety.
"""

from __future__ import annotations
import argparse
import math
from pathlib import Path
from h5_experiment_registry import sha, snapshot, need, hex64
from independent_soak_replay import strict_json
import json
import hashlib


def assess(report, journal, plan_pin, head, report_pin):
    need(hex64(report_pin) and sha(report) == report_pin, "H8_REPORT_PIN")
    need(
        report["schema"] == "h6_registered_strategy_run_v1"
        and report["plan_sha256"] == plan_pin
        and report["registry_head_sha256"] == head
        and report["return_basis"] == "SEGMENTED_WHOLE_EPOCH_RETURNS_NOT_REGULAR_GRID"
        and all(
            report[k] is False
            for k in (
                "holdout_read",
                "selection_performed",
                "actual_fills_verified",
                "oos_approved",
                "alpha_confirmed",
                "execution_authorized",
            )
        ),
        "H8_REPORT_MODE",
    )
    reg = snapshot(journal, plan_pin, head)
    plan = reg["plan"]
    cat = report["catalog"]
    need(
        reg["all_terminal"]
        and report["all_complete"] is reg["all_complete"]
        and report["plan"] == plan
        and cat["schema"] == "h4_native_cross_session_catalog_v1"
        and cat["catalog_sha256"] == plan["dataset_catalog_sha256"]
        and sha({k: v for k, v in cat.items() if k != "catalog_sha256"})
        == cat["catalog_sha256"],
        "H8_PLAN_CATALOG",
    )
    sessions = cat["sessions"]
    trials = plan["trials"]
    results = report["results"]
    need(
        1 <= len(sessions) <= 16
        and all(s["role"] == "DEVELOPMENT" for s in sessions)
        and len(results) == len(trials)
        and [x["trial_id"] for x in results] == [t["trial_id"] for t in trials],
        "H8_INVENTORY",
    )
    synthetic = plan["source_kind"] == "SYNTHETIC_TEST_ONLY"
    need(
        cat["source_kind"]
        == ("SYNTHETIC_TEST_ONLY" if synthetic else "ARCHIVE_REPLAY"),
        "H8_SOURCE_KIND",
    )
    output = []
    for t, r in zip(trials, results, strict=True):
        tid = t["trial_id"]
        state = reg["state"][tid]
        receipt = reg["results"][tid]
        need(r["status"] == state and state in ("COMPLETE", "FAILED"), "H8_TRIAL_STATE")
        mean = None
        trades = 0
        if state == "COMPLETE":
            need(
                sha(r) == receipt["result_sha256"]
                and len(r["sessions"]) == len(sessions),
                "H8_RESULT_PIN",
            )
            segment_hash = sha(
                {
                    "basis": "SEGMENTED_WHOLE_EPOCH_RETURNS_NOT_REGULAR_GRID",
                    "sessions": [s["return_series_sha256"] for s in r["sessions"]],
                }
            )
            need(segment_hash == receipt["return_series_sha256"], "H8_SERIES_PIN")
            for a, b in zip(r["sessions"], sessions, strict=True):
                need(
                    a["source_manifest_sha256"] == b["source_manifest_sha256"]
                    and a["dataset_receipt_sha256"] == b["dataset_receipt_sha256"]
                    and type(a["net_return"]) in (int, float)
                    and math.isfinite(a["net_return"])
                    and a["net_return"] > -1
                    and type(a["closed_roundtrips"]) is int
                    and a["closed_roundtrips"] >= 0
                    and a["initial_cash_reset"] is True,
                    "H8_SESSION",
                )
            mean = sum(s["net_return"] for s in r["sessions"]) / len(sessions)
            trades = sum(s["closed_roundtrips"] for s in r["sessions"])
            need(
                math.isclose(
                    mean, r["mean_session_net_return"], rel_tol=1e-12, abs_tol=1e-14
                ),
                "H8_MEAN",
            )
        else:
            need(r["reason"] == receipt["reason"], "H8_FAILURE")
        reasons = []
        if synthetic:
            reasons.append("SYNTHETIC_ONLY")
        if not reg["all_complete"]:
            reasons.append("INCOMPLETE_TRIAL_INVENTORY")
        if state != "COMPLETE":
            reasons.append("TRIAL_FAILED")
        elif trades == 0:
            reasons.append("NO_COMPLETED_TRADES")
        elif mean <= 0:
            reasons.append("NONPOSITIVE_DEVELOPMENT_RETURN")
        else:
            reasons.append("POSITIVE_DEVELOPMENT_ONLY")
        reasons.extend(
            [
                "SAME_POLICY_HOLDOUT_UNVERIFIED",
                "REAL_FILL_COST_UNCALIBRATED",
                "LIVE_RESILIENCE_UNVERIFIED",
                "MANUAL_APPROVAL_UNAVAILABLE",
            ]
        )
        verdict = (
            "REJECTED_DEVELOPMENT"
            if not synthetic and state == "COMPLETE" and trades > 0 and mean <= 0
            else "BLOCKED"
        )
        output.append(
            {
                "trial_id": tid,
                "parameters": t["parameters"],
                "trial_status": state,
                "verdict": verdict,
                "mean_session_net_return": mean,
                "closed_roundtrips": trades,
                "reasons": reasons,
            }
        )
    return {
        "schema": "h8_strategy_qualification_v1",
        "status": "NO_APPROVAL",
        "source_kind": plan["source_kind"],
        "plan_sha256": plan_pin,
        "registry_head_sha256": head,
        "development_report_sha256": report_pin,
        "dataset_catalog_sha256": cat["catalog_sha256"],
        "session_count": len(sessions),
        "trial_count": len(output),
        "trials": output,
        "registry_verified": True,
        "same_policy_oos_verified": False,
        "statistical_qualification": False,
        "shadow_authorized": False,
        "paper_authorized": False,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report", type=Path)
    p.add_argument("journal", type=Path)
    p.add_argument("--plan-sha256", required=True)
    p.add_argument("--head-sha256", required=True)
    p.add_argument("--report-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    need(a.report.stat().st_size <= 4_000_000, "H8_REPORT_SIZE")
    r = assess(
        strict_json(a.report.read_bytes()),
        a.journal,
        a.plan_sha256,
        a.head_sha256,
        a.report_sha256,
    )
    with a.output.open("x") as f:
        json.dump(r, f, sort_keys=True, allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
