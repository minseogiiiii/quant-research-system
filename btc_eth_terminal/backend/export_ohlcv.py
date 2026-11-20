"""Conservative mapping from a local research CSV/.csv.gz to Funnest OHLCV.

Only user-specified fields are copied. No price imputation, resampling, forward
labels, strategy execution, or return estimation. Never changes original files.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import os
import tempfile
from pathlib import Path

from bridge import DATA_ROOT, FORWARD_FIELD, InputError, _num, _time_ms, parse_bars


def bridge_forward_field(name: str) -> bool:
    return bool(FORWARD_FIELD.search(name))


def convert(*, src: Path, dst: Path, columns: dict[str, str], overwrite: bool = False) -> dict:
    source = src.resolve()
    target = dst.resolve()
    if not source.is_file() or source == target:
        raise InputError("Input must exist, and input/output must differ")
    if not target.is_relative_to(DATA_ROOT):
        raise InputError("Output must be under backend/data")
    if any("future_labels" in part.lower() for part in source.parts) or any(
        "future_labels" in part.lower() for part in target.parts
    ):
        raise InputError("Future labels are evaluation-only, not an OHLCV datafeed")
    if any(bridge_forward_field(key) for key in columns.values()):
        raise InputError("Explicit OHLCV mapping cannot use future/target/label columns")
    if target.exists() and not overwrite:
        raise InputError("Output already exists; --overwrite required for a deliberate replacement")
    if target.suffix.lower() != ".csv":
        raise InputError("Output must have .csv extension")
    initial_digest = _sha(source)
    read = gzip.open if source.name.endswith(".gz") else open
    out: list[dict[str, object]] = []
    with read(source, "rt", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp, strict=True)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise InputError("Repeated input columns")
        missing = set(columns.values()) - set(fieldnames)
        if missing:
            raise InputError(f"Input lacks explicitly selected columns: {sorted(missing)}")
        prev = -1
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise InputError(f"Malformed source CSV row {line}")
            t = _time_ms(row[columns["time"]])
            o, h, lo, c = [_num(row[columns[k]], k) for k in ("open", "high", "low", "close")]
            vol = _num(row[columns["volume"]], "volume") if "volume" in columns else None
            if t <= prev or min(o, h, lo, c) <= 0 or lo > min(o, c) or h < max(o, c) or lo > h or (vol is not None and vol < 0):
                raise InputError(f"Invalid/duplicate/unordered OHLCV at source row {line}")
            record: dict[str, object] = {"time": t, "open": o, "high": h, "low": lo, "close": c,
                                         "volume": "" if vol is None else vol}
            if "available_at" in columns:
                available = _time_ms(row[columns["available_at"]])
                # This bridge treats explicitly mapped 5-minute candle
                # timestamps as bar OPEN times. A completed OHLCV bar cannot
                # be known before its 5-minute close. No arrival time is
                # inferred if the source lacks availability data.
                if available < t + 300_000:
                    raise InputError(
                        f"Row {line}: completed-bar available_at precedes the bar close"
                    )
                record["available_at"] = available
            out.append(record)
            prev = t
    if not out:
        raise InputError("No input rows; refused output")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".quant-export-", suffix=".csv", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=["time", "open", "high", "low", "close", "volume"] +
                                    (["available_at"] if "available_at" in columns else []))
            writer.writeheader()
            writer.writerows(out)
            fp.flush()
            os.fsync(fp.fileno())
        # Parse and hash the exact temporary bytes before an atomic rename.
        parsed, digest = parse_bars(
            str(Path(temp).relative_to(DATA_ROOT)), completed_bar_ms=300_000
        )
        if _sha(source) != initial_digest:
            raise InputError("Input changed during export; refusing to publish stale/ambiguous output")
        if target.exists() and not overwrite:
            raise InputError("Concurrent output creation; refusing overwrite")
        os.replace(temp, target)
        temp = ""
        return {"rows": len(parsed), "derived_csv_sha256": digest, "input_sha256": initial_digest,
                "time_range": [parsed[0]["time"], parsed[-1]["time"]],
                "asof_supported": "available_at" in columns}
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)


def _sha(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for k in ("time", "open", "high", "low", "close"):
        parser.add_argument("--" + k.replace("_", "-"), required=True)
    parser.add_argument("--volume")
    parser.add_argument("--available-at", help="Explicit time each complete bar became observable")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    columns = {k: getattr(args, k) for k in ("time", "open", "high", "low", "close", "volume", "available_at") if getattr(args, k)}
    try:
        result = convert(src=args.input, dst=args.output, columns=columns, overwrite=args.overwrite)
    except InputError as exc:
        parser.error(str(exc))
    import json
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
