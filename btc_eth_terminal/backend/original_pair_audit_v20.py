"""Evidence-first, read-only BTC/ETH 5-minute feature/future-label paired audit.

Requires explicit mapping and source identity. It refuses to infer venue, symbol,
label horizon, availability time or provider authenticity from filenames.
No features or future labels are generated, no dataset is admitted to model/OOS.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

MAX_COMPRESSED = 2_000_000_000
MAX_ROWS = 4_000_000
MAX_UNCOMPRESSED = 8_000_000_000
STEP_MS = 300_000


class ProvenanceError(ValueError):
    pass


def reject(message):
    raise ProvenanceError(message)


def text_json(data):
    def pairs(values):
        out = {}
        for k, v in values:
            if k in out:
                reject("Duplicate JSON key: " + k)
            out[k] = v
        return out

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=lambda x: reject("Nonfinite JSON token"),
    )


def digest(path):
    h = sha256()
    with path.open("rb") as fp:
        for piece in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def time_ms(value, time_format):
    if not isinstance(value, str) or not value:
        reject("Empty timestamp")
    if time_format == "epoch_ms":
        if not value.isdecimal():
            reject("Time is not explicit epoch milliseconds")
        out = int(value)
    elif time_format == "iso_utc":
        if not value.endswith("Z"):
            reject("Time not explicit UTC ISO")
        dt = datetime.fromisoformat(value[:-1] + "+00:00")
        if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
            reject("Invalid UTC timestamp")
        if dt.microsecond % 1000:
            reject("Submillisecond time not supported")
        out = int(dt.timestamp() * 1000)
    else:
        reject("Timestamp encoding must be explicit epoch_ms or iso_utc")
    if not 1_500_000_000_000 < out < 4_000_000_000_000:
        reject("Timestamp outside bounded supported UTC range")
    return out


def file_in_root(root, name):
    if (
        not isinstance(name, str)
        or name.startswith("/")
        or "\\" in name
        or Path(name).is_absolute()
        or any(v in ("", ".", "..") for v in name.split("/"))
    ):
        reject("Unsafe or ambiguous relative file path")
    target = root / name
    if any(
        p.is_symlink() for p in [target, *list(target.parents)[: len(Path(name).parts)]]
    ):
        reject("Symlink in source path")
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        reject("Missing/out-of-root original file: " + name)
    if target.stat().st_size == 0 or target.stat().st_size > MAX_COMPRESSED:
        reject("Original file size outside cap")
    return target


def validate_entry(root, entry, expected_suffix, time_format, kind):
    if not isinstance(entry, dict):
        reject("Missing feature/label entry")
    allowed = {
        "path",
        "sha256",
        "bar_open_column",
        "decision_at_column",
        "observed_at_column",
        "available_at_column",
        "label_end_column",
        "horizon_ms",
        "raw_source_path",
        "raw_source_sha256",
    }
    if set(entry).difference(allowed):
        reject("Unknown manifest entry keys")
    name = entry.get("path")
    if not isinstance(name, str) or not name.endswith(expected_suffix):
        reject("Wrong original file suffix")
    source = file_in_root(root, name)
    original = digest(source)
    if entry.get("sha256") != original:
        reject("Source file digest does not match explicit manifest")
    if any(k in entry for k in ("raw_source_path", "raw_source_sha256")):
        rawpath = entry.get("raw_source_path")
        if not rawpath or not isinstance(entry.get("raw_source_sha256"), str):
            reject("Partial original market source evidence")
        if rawpath == name or rawpath.endswith(
            ("_features_5m.csv.gz", "_future_labels.csv.gz")
        ):
            reject("A derived research CSV cannot self-attest market-source provenance")
        if digest(file_in_root(root, rawpath)) != entry["raw_source_sha256"]:
            reject("Market-source archive digest mismatch")
        archive_matched = True
    else:
        archive_matched = False
    required = (
        ["bar_open_column", "decision_at_column"]
        if kind == "feature"
        else ["bar_open_column", "decision_at_column", "label_end_column", "horizon_ms"]
    )
    for key in required:
        if not entry.get(key):
            reject("Missing mandatory mapping: " + key)
    if kind == "label" and (
        type(entry["horizon_ms"]) is not int
        or not 1 <= entry["horizon_ms"] <= 7 * 24 * 3600 * 1000
    ):
        reject("Invalid explicit label horizon")
    columns = [
        entry[k]
        for k in (
            "bar_open_column",
            "decision_at_column",
            "observed_at_column",
            "available_at_column",
            "label_end_column",
        )
        if entry.get(k)
    ]
    if len(columns) != len(set(columns)):
        reject("Time mappings alias distinct semantics")
    hash_before = original
    count = 0
    timeline = sha256()
    previous = None
    first = last = None
    gaps = 0
    avail_checked = False
    max_size = 0
    opener = gzip.open if name.endswith(".gz") else open
    with opener(source, "rt", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        fields = reader.fieldnames or []
        if (
            not fields
            or len(fields) != len(set(fields))
            or any(not x.strip() for x in fields)
        ):
            reject("Ambiguous feature/label header")
        if any(x not in fields for x in columns):
            reject("Declared time column absent from original file")
        lc = {x.lower() for x in fields}
        if kind == "feature" and any(
            x.startswith(("future", "label", "target", "forward", "lead", "next_"))
            or "_future" in x
            or "_label" in x
            or "_target" in x
            for x in lc
        ):
            reject("Likely future-target fields inside feature source")
        for row in reader:
            count += 1
            if count > MAX_ROWS:
                reject("Excessive row count")
            if None in row or any(v is None for v in row.values()):
                reject("Malformed CSV row")
            max_size += sum(len(str(v)) for v in row.values())
            if max_size > MAX_UNCOMPRESSED:
                reject("Decompression expansion exceeds cap")
            opening = time_ms(row[entry["bar_open_column"]], time_format)
            decision = time_ms(row[entry["decision_at_column"]], time_format)
            if opening % STEP_MS:
                reject("Bar not on 5-minute grid")
            if previous is not None:
                if opening <= previous:
                    reject("Out-of-order/duplicate opening bar")
                gaps += (opening - previous) // STEP_MS - 1
            if decision < opening:
                reject("Decision clock before bar open")
            if kind == "feature":
                if entry.get("observed_at_column"):
                    observed = time_ms(row[entry["observed_at_column"]], time_format)
                    if observed > decision:
                        reject("Future-observed feature read at decision time")
                if entry.get("available_at_column"):
                    available = time_ms(row[entry["available_at_column"]], time_format)
                    if available > decision:
                        reject("Future-available feature read at decision time")
                    if entry.get("observed_at_column") and observed > available:
                        reject("Feature recorded available before observation")
                    avail_checked = True
            else:
                end = time_ms(row[entry["label_end_column"]], time_format)
                if end != decision + entry["horizon_ms"]:
                    reject("Label end differs from declared horizon and decision clock")
                if end <= decision:
                    reject("Future label not after decision")
                if entry.get("available_at_column"):
                    available = time_ms(row[entry["available_at_column"]], time_format)
                    if available < end:
                        reject("Label claims availability before future horizon")
                    avail_checked = True
            timeline.update(f"{opening}:{decision}\n".encode("ascii"))
            if first is None:
                first = opening
            last = opening
            previous = opening
    if gaps:
        reject("Discontinuous 5-minute bars: explicit cohort split required")
    if digest(source) != hash_before:
        reject("Original mutated during audit")
    if count == 0:
        reject("Empty original data")
    return {
        "file": name,
        "original_sha256": original,
        "rows": count,
        "columns": fields,
        "first_open_ms": first,
        "last_open_ms": last,
        "missing_bar_steps": gaps,
        "timestamp_order_checks_passed": True,
        "timeline_digest": timeline.hexdigest(),
        "available_at_values_order_checked": avail_checked,
        "available_at_independently_attested": False,
        "raw_market_source_bytes_sha_matched": archive_matched,
    }


def audit(root: Path, manifest: dict):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        reject("Root source directory invalid/symlink")
    if manifest.get("schema") != "btc_eth_original_research_pair_v20":
        reject("Wrong provenance manifest schema")
    required = (
        "venue",
        "market_type",
        "product_id",
        "bar_seconds",
        "timestamp_format",
        "feature",
        "future_labels",
        "source_provider",
    )
    if any(k not in manifest for k in required):
        reject("Missing explicit metadata for source/contract")
    if (
        manifest["venue"] not in ("Coinbase", "Binance", "Kraken", "OtherDeclaredVenue")
        or manifest["market_type"] not in ("spot", "perpetual")
        or not isinstance(manifest["source_provider"], str)
        or not manifest["source_provider"].strip()
        or manifest["product_id"] not in ("BTC-USD", "ETH-USD", "BTCUSDT", "ETHUSDT")
        or manifest["bar_seconds"] != 300
    ):
        reject("Incomplete/wrong venue, contract or interval identity")
    if manifest["venue"] == "Coinbase" and manifest["market_type"] != "spot":
        reject("Current Coinbase BTC-USD/ETH-USD source is spot, not perpetual")
    if manifest["venue"] == "Coinbase" and manifest["product_id"] not in (
        "BTC-USD",
        "ETH-USD",
    ):
        reject("Coinbase source identity inconsistent")
    if manifest["venue"] == "Binance" and manifest["product_id"] not in (
        "BTCUSDT",
        "ETHUSDT",
    ):
        reject("Binance source identity inconsistent")
    format_ = manifest["timestamp_format"]
    if format_ not in ("iso_utc", "epoch_ms"):
        reject("Time encoding unsupported")
    f = validate_entry(
        root, manifest["feature"], "_features_5m.csv.gz", format_, "feature"
    )
    label_entry = validate_entry(
        root, manifest["future_labels"], "_future_labels.csv.gz", format_, "label"
    )
    fp = Path(f["file"]).name.removesuffix("_features_5m.csv.gz")
    lp = Path(label_entry["file"]).name.removesuffix("_future_labels.csv.gz")
    if not fp or fp != lp:
        reject("Feature and label original filename cohort prefixes disagree")
    if f["file"] == label_entry["file"] or f["timeline_digest"] != label_entry["timeline_digest"]:
        reject("Features/labels not separate or 5m pair row cohort mismatch")
    return {
        "status": "STRUCTURAL_PAIR_AUDIT_ONLY_NOT_PIT_ATTESTATION",
        "market_identity_claimed_by_manifest_not_independently_verified": {
            "venue": manifest["venue"],
            "market_type": manifest["market_type"],
            "product_id": manifest["product_id"],
            "source_provider": manifest["source_provider"],
        },
        "label_horizon_ms_explicit": manifest["future_labels"]["horizon_ms"],
        "feature": f,
        "future_labels": label_entry,
        "source_provider_authenticity_verified": False,
        "feature_availability_original_capture_verified": False,
        "future_label_never_allowed_as_signal_feature": True,
        "eligible_for_OOS_research_without_independent_provenance": False,
        "eligible_for_live": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if (
        args.output.suffix != ".json"
        or args.output.is_symlink()
        or args.output.resolve()
        in {
            args.manifest.resolve(),
            (
                args.root
                / str(
                    text_json(args.manifest.read_bytes())
                    .get("feature", {})
                    .get("path", "")
                )
            ).resolve(),
            (
                args.root
                / str(
                    text_json(args.manifest.read_bytes())
                    .get("future_labels", {})
                    .get("path", "")
                )
            ).resolve(),
        }
    ):
        raise SystemExit(
            "Unsafe report destination; original research bytes must not be overwritten"
        )
    try:
        result = audit(args.root, text_json(args.manifest.read_bytes()))
    except (ProvenanceError, OSError, csv.Error, ValueError, EOFError) as exc:
        result = {
            "status": "REJECTED_ORIGINAL_PAIR_NOT_ADMITTED",
            "error": str(exc)[:500],
            "allowed_into_feature_store": False,
            "eligible_for_OOS": False,
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit(2)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "files": [result["feature"]["file"], result["future_labels"]["file"]],
            }
        )
    )


if __name__ == "__main__":
    main()
