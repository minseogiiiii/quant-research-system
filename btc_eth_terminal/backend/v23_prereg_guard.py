"""v23 no-alpha research registration and chronological split safety controls.

Validates a predeclared P2 *design*, not the existence of tradeable alpha.
A locked draft is tamper-evident only on the same host; it does not attest
market source provenance, protect against an administrator, or prove blind OOS.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re

SCHEMA = "V23_RESEARCH_PROTOCOL_V1"
FAMILY = "EVENT_TIME_LIQUIDITY_RECOVERY"
ALLOWED_BASELINES = frozenset({"NO_TRADE", "OFI", "MICROPRICE", "IMBALANCE", "TREND", "VOLATILITY"})
ALLOWED_FEATURES = frozenset({"L1_OFI_5M", "MICROPRICE", "BEST_IMBALANCE", "TREND", "REALIZED_VOL"})
MAX_JSON = 64_000
DAY = 86_400_000


class ProtocolReject(ValueError):
    pass


def reject(message):
    raise ProtocolReject(message)


def read_json(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_JSON:
        reject("Invalid protocol source path or size")
    raw = p.read_bytes()
    if len(raw) > MAX_JSON:
        reject("Protocol size exceeds bound")
    def unique(pairs):
        x = {}
        for k, v in pairs:
            if k in x:
                reject("Duplicate JSON key")
            x[k] = v
        return x
    def no_constant(token):
        reject("Nonfinite JSON constant")
    try:
        obj = json.loads(raw, object_pairs_hook=unique, parse_constant=no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolReject("Invalid protocol JSON") from exc
    return obj, raw


def utc_ms(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        reject("UTC boundaries must use explicit second-resolution Z clock")
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProtocolReject("Invalid UTC boundary") from exc
    if dt.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        reject("Noncanonical UTC boundary")
    return int(dt.timestamp() * 1000)


def validate_protocol(obj):
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
        reject("Incorrect preregistration schema")
    if obj.get("research_family") != FAMILY:
        reject("Research family changed")
    if obj.get("data_role") != "NOT_YET_ADMITTED_TO_P2":
        reject("Do not declare an unaudited source eligible")
    if obj.get("real_orders_authorized") is not False or obj.get("blind_results_inspected") is not False:
        reject("Live trade permission or blind results already read")
    if obj.get("execution_model") != "PUBLIC_L2_HYPOTHETICAL_UNVERIFIED":
        reject("Unknown execution model cannot be promoted")
    if obj.get("original_source_pit_verified") is not False:
        reject("Original source PIT evidence not established")
    if obj.get("independent_multi_day_data_verified") is not False:
        reject("Missing independent multi-day evidence")
    baselines, features = obj.get("baselines"), obj.get("features")
    if (not isinstance(baselines, list) or not baselines or
            len(baselines) != len(set(map(str, baselines))) or
            "NO_TRADE" not in baselines or "OFI" not in baselines or
            "MICROPRICE" not in baselines or not set(baselines) <= ALLOWED_BASELINES):
        reject("Missing or unbounded competing baselines")
    if (not isinstance(features, list) or not features or
            len(features) != len(set(map(str, features))) or
            not set(features) <= ALLOWED_FEATURES):
        reject("Unbounded features")
    hs = obj.get("label_horizons_ms")
    if (not isinstance(hs, list) or not hs or len(hs) != len(set(map(str, hs))) or
            any(type(h) is not int or h <= 0 or h > DAY for h in hs)):
        reject("Invalid horizon declarations")
    embargo = obj.get("embargo_ms")
    if type(embargo) is not int or embargo < max(hs):
        reject("Embargo must cover largest label horizon")
    schedule = obj.get("partition_schedule")
    if not isinstance(schedule, dict) or set(schedule) != {"train", "validation", "blind"}:
        reject("Incomplete partition schedule")
    windows = {}
    for part in ("train", "validation", "blind"):
        w = schedule[part]
        if not isinstance(w, dict) or set(w) != {"start", "end"}:
            reject("Incomplete partition clock")
        a,b = utc_ms(w["start"]), utc_ms(w["end"])
        if a >= b or a % DAY != 0 or b % DAY != 0:
            reject("Partitions require distinct whole UTC days")
        windows[part] = (a,b)
    if windows["train"][1] + embargo > windows["validation"][0] or \
       windows["validation"][1] + embargo > windows["blind"][0]:
        reject("Partitions overlap or lack declared embargo")
    if any(end - start < 2 * DAY for start, end in windows.values()):
        reject("Multiple distinct days required per partition")
    if not isinstance(obj.get("trial_budget"), int) or type(obj["trial_budget"]) is not int or not 1 <= obj["trial_budget"] <= 30:
        reject("Trial budget not bounded")
    if obj.get("source_identifier") != "NEW_INDEPENDENT_SOURCE_REQUIRED":
        reject("Cannot reuse one-hour engineering fixture as P2 source")
    return windows


def evaluate_structural_rows(rows, protocol):
    """Test partition mechanics only. Never return blind labels or alpha estimates.

    `rows` must be SYNTHETIC fixtures in this version: external PIT evidence
    is still absent. No caller may make these rows eligible for alpha claims.
    """
    windows = validate_protocol(protocol)
    if not isinstance(rows, list):
        reject("Rows must be a list")
    used = set()
    result = {"train": [], "validation": [], "blind_metadata": [], "purged": [], "outside": []}
    for row in rows:
        if not isinstance(row, dict):
            reject("Malformed observation")
        allowed = {"instrument", "market_type", "source_type", "decision_ms", "feature_max_received_ms",
                   "label_end_ms", "horizon_ms", "source_seq", "future_label_value"}
        if set(row) - allowed:
            reject("Unknown field may hide target leakage")
        if row.get("source_type") != "SYNTHETIC_TEST_ONLY" or row.get("market_type") != "SPOT" or row.get("instrument") not in ("BTC-USD", "ETH-USD"):
            reject("External market rows not admitted as PIT evidence")
        t, feature, end, h, seq = (row.get(k) for k in
            ("decision_ms", "feature_max_received_ms", "label_end_ms", "horizon_ms", "source_seq"))
        if any(type(x) is not int for x in (t, feature, end, h, seq)):
            reject("Noninteger or missing event clocks/sequence")
        if feature > t or end != t+h or h not in protocol["label_horizons_ms"]:
            reject("Future feature, horizon drift, or unknown target horizon")
        key = (row["instrument"], t, h)
        if key in used:
            reject("Duplicate observation")
        used.add(key)
        role = next((k for k,(a,b) in windows.items() if a <= t < b), None)
        if role is None:
            result["outside"].append(key)
            continue
        if role == "blind":
            if "future_label_value" in row:
                reject("Blind labels supplied in model-selection path")
            result["blind_metadata"].append(key)
            continue
        # Require no label overlap or trailing ambiguous period.
        if end + protocol["embargo_ms"] > windows[role][1]:
            result["purged"].append(key)
            continue
        if "future_label_value" not in row:
            reject("Missing synthetic train/validation label")
        x = row["future_label_value"]
        if type(x) not in (int, float) or x != x or abs(x) == float("inf"):
            reject("Invalid synthetic label")
        result[role].append(key)
    return {
        "status": "SYNTHETIC_PARTITION_CHECK_ONLY_NOT_P2_ALPHA",
        "train_rows": len(result["train"]),
        "validation_rows": len(result["validation"]),
        "blind_metadata_rows": len(result["blind_metadata"]),
        "purged_rows": len(result["purged"]),
        "outside_rows": len(result["outside"]),
        "blind_label_read_or_evaluated": False,
        "original_market_feature_pit_verified": False,
        "eligible_for_real_research_promotion": False,
    }


def lock_draft_once(source: Path, destination: Path):
    """Write-once local tamper marker; not a substitute for access controls."""
    plan, raw = read_json(source)
    validate_protocol(plan)
    destination = Path(destination)
    if destination.is_symlink() or destination.exists() or not destination.parent.is_dir():
        reject("Draft lock already exists or destination invalid")
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    digest = sha256(canonical).hexdigest()
    body = json.dumps({
        "schema": "V23_DRAFT_LOCK_V1", "protocol_sha256": digest,
        "protocol": plan,
        "verified_data_eligible": False,
        "status": "WRITE_ONCE_LOCAL_PREREG_DRAFT_NOT_SEALED_BLIND",
    }, sort_keys=True, indent=2).encode() + b"\n"
    fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return digest


def verify_lock(path):
    obj, raw = read_json(path)
    if obj.get("schema") != "V23_DRAFT_LOCK_V1" or obj.get("verified_data_eligible") is not False:
        reject("Invalid or promoted preregistration draft")
    p = obj.get("protocol")
    validate_protocol(p)
    canonical = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if sha256(canonical).hexdigest() != obj.get("protocol_sha256"):
        reject("Draft protocol changed since lock")
    return obj["protocol_sha256"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan", type=Path)
    ap.add_argument("--lock-draft", type=Path)
    ap.add_argument("--verify-lock", action="store_true")
    args = ap.parse_args()
    if args.verify_lock:
        print("LOCKED_LOCAL_DRAFT_NOT_BLIND_CERTIFICATE", verify_lock(args.plan))
    elif args.lock_draft:
        print("WRITE_ONCE_LOCAL_DRAFT", lock_draft_once(args.plan, args.lock_draft))
    else:
        plan, _ = read_json(args.plan)
        validate_protocol(plan)
        print("STRUCTURAL_PREREG_DRAFT_VALID_EXTERNAL_PIT_OOS_BLOCKED")


if __name__ == "__main__":
    main()
