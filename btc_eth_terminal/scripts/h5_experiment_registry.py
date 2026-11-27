"""Offline append-only trial journal with externally pinned plan and terminal head.

Local tamper evidence, not an authorization system or proof of holdout secrecy.
Each candidate is frozen before execution. Failed trials remain in the inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from independent_soak_replay import strict_json

MAX_TRIALS = 128
MAX_DOCUMENT = 2_000_000
ZERO = "0" * 64


def need(ok, why):
    if not ok:
        raise ValueError("H5_" + why)


def canonical(x):
    return json.dumps(
        x, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha(x):
    return hashlib.sha256(canonical(x)).hexdigest()


def hex64(x):
    return type(x) is str and re.fullmatch(r"[0-9a-f]{64}", x) is not None


def validate_plan(p):
    keys = {
        "schema",
        "source_kind",
        "dataset_catalog_sha256",
        "strategy_code_sha256",
        "cost_policy_sha256",
        "split_policy_sha256",
        "family",
        "trials",
        "seed",
    }
    need(
        type(p) is dict
        and set(p) == keys
        and p["schema"] == "h5_experiment_plan_v1"
        and p["source_kind"] in ("SYNTHETIC_TEST_ONLY", "DEVELOPMENT")
        and all(
            hex64(p[k])
            for k in (
                "dataset_catalog_sha256",
                "strategy_code_sha256",
                "cost_policy_sha256",
                "split_policy_sha256",
            )
        )
        and type(p["family"]) is str
        and 0 < len(p["family"]) <= 128
        and type(p["seed"]) is int
        and 0 <= p["seed"] < 2**32
        and type(p["trials"]) is list
        and 1 <= len(p["trials"]) <= MAX_TRIALS,
        "PLAN",
    )
    seen = set()
    for t in p["trials"]:
        need(
            type(t) is dict
            and set(t) == {"trial_id", "parameters"}
            and type(t["parameters"]) is dict
            and t["trial_id"] == sha(t["parameters"])
            and t["trial_id"] not in seen,
            "TRIAL_ID",
        )
        seen.add(t["trial_id"])
    need(len(canonical(p)) <= MAX_DOCUMENT, "PLAN_SIZE")
    return p


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    return db


def create(path, plan):
    path = Path(path)
    validate_plan(plan)
    with path.open("xb"):
        pass
    with connect(path) as db:
        db.execute(
            "CREATE TABLE journal (idx INTEGER PRIMARY KEY, prev TEXT NOT NULL, "
            "body BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TRIGGER no_update BEFORE UPDATE ON journal BEGIN "
            "SELECT RAISE(ABORT,'append only'); END"
        )
        db.execute(
            "CREATE TRIGGER no_delete BEFORE DELETE ON journal BEGIN "
            "SELECT RAISE(ABORT,'append only'); END"
        )
        body = {"event": "PLAN", "plan": plan}
        head = sha({"idx": 0, "prev": ZERO, "body": body})
        db.execute(
            "INSERT INTO journal VALUES (?,?,?,?)", (0, ZERO, canonical(body), head)
        )
    return sha(plan), head


def inspect_db(db, plan_pin):
    need(hex64(plan_pin), "PLAN_PIN")
    state = {}
    results = {}
    plan = None
    prev = ZERO
    count = 0
    for idx, old, raw, head in db.execute(
        "SELECT idx,prev,body,digest FROM journal ORDER BY idx"
    ):
        need(idx == count and old == prev and len(raw) <= MAX_DOCUMENT, "CHAIN")
        body = strict_json(raw)
        need(
            canonical(body) == raw
            and head == sha({"idx": idx, "prev": old, "body": body}),
            "HASH",
        )
        if idx == 0:
            need(set(body) == {"event", "plan"} and body["event"] == "PLAN", "GENESIS")
            plan = validate_plan(body["plan"])
            need(sha(plan) == plan_pin, "PLAN_MISMATCH")
            state = {t["trial_id"]: "PLANNED" for t in plan["trials"]}
        else:
            tid = body.get("trial_id")
            kind = body.get("event")
            need(tid in state, "UNKNOWN_TRIAL")
            if kind == "START":
                need(
                    set(body) == {"event", "trial_id"} and state[tid] == "PLANNED",
                    "START_STATE",
                )
                state[tid] = "RUNNING"
            elif kind == "COMPLETE":
                need(
                    set(body)
                    == {"event", "trial_id", "return_series_sha256", "result_sha256"}
                    and state[tid] == "RUNNING"
                    and hex64(body["return_series_sha256"])
                    and hex64(body["result_sha256"]),
                    "COMPLETE_STATE",
                )
                state[tid] = "COMPLETE"
                results[tid] = body
            elif kind == "FAIL":
                need(
                    set(body) == {"event", "trial_id", "reason"}
                    and state[tid] == "RUNNING"
                    and type(body["reason"]) is str
                    and re.fullmatch(r"[A-Z0-9_]{1,80}", body["reason"]),
                    "FAIL_STATE",
                )
                state[tid] = "FAILED"
                results[tid] = body
            else:
                raise ValueError("H5_EVENT")
        prev = head
        count += 1
        need(count <= 1 + 2 * MAX_TRIALS, "EVENT_BOUND")
    need(plan is not None, "EMPTY")
    return {
        "plan": plan,
        "plan_sha256": plan_pin,
        "head_sha256": prev,
        "event_count": count,
        "state": state,
        "results": results,
        "all_terminal": all(v in ("COMPLETE", "FAILED") for v in state.values()),
        "all_complete": all(v == "COMPLETE" for v in state.values()),
    }


def snapshot(path, plan_pin, expected_head):
    need(
        Path(path).is_file() and not Path(path).is_symlink() and hex64(expected_head),
        "JOURNAL_PATH_PIN",
    )
    with connect(path) as db:
        db.execute("BEGIN")
        r = inspect_db(db, plan_pin)
        need(r["head_sha256"] == expected_head, "HEAD_MISMATCH")
        return r


def append(path, plan_pin, expected_head, body):
    need(Path(path).is_file() and not Path(path).is_symlink(), "JOURNAL_PATH")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        r = inspect_db(db, plan_pin)
        need(r["head_sha256"] == expected_head, "HEAD_MISMATCH")
        idx = r["event_count"]
        head = sha({"idx": idx, "prev": expected_head, "body": body})
        db.execute(
            "INSERT INTO journal VALUES (?,?,?,?)",
            (idx, expected_head, canonical(body), head),
        )
        # Validate transaction including state transition before committing.
        inspect_db(db, plan_pin)
    return head


def series_hash(returns, timestamps, interval_ms):
    return sha(
        {
            "return_basis": "REGULAR_NET_PORTFOLIO_RETURNS",
            "returns": returns,
            "timestamps": timestamps,
            "interval_ms": interval_ms,
        }
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create")
    c.add_argument("plan", type=Path)
    for name in ("append", "inspect"):
        q = sub.add_parser(name)
        q.add_argument("--plan-sha256", required=True)
        q.add_argument("--head-sha256", required=True)
        if name == "append":
            q.add_argument("event", type=Path)
    a = p.parse_args()
    if a.action == "create":
        need(a.plan.stat().st_size <= MAX_DOCUMENT, "DOCUMENT_SIZE")
        pin, head = create(a.journal, strict_json(a.plan.read_bytes()))
        result = {"plan_sha256": pin, "head_sha256": head}
    elif a.action == "append":
        need(a.event.stat().st_size <= MAX_DOCUMENT, "DOCUMENT_SIZE")
        result = {
            "head_sha256": append(
                a.journal,
                a.plan_sha256,
                a.head_sha256,
                strict_json(a.event.read_bytes()),
            )
        }
    else:
        result = snapshot(a.journal, a.plan_sha256, a.head_sha256)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
