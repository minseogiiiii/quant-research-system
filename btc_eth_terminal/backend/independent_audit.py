"""Independent, stdlib-only consistency audit of archived Coinbase candle bytes.

This deliberately does not import bridge, fetch_coinbase_5m, or fetch_btc_eth.
Two implementations agreeing reduces coding mistakes; neither proves that bytes
actually originated at Coinbase. No trading, price forecasts, or PIT claims.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRODUCTS = {"BTC-USD": "btc_coinbase_spot_5m", "ETH-USD": "eth_coinbase_spot_5m"}
ROW_NAMES = ["time", "open", "high", "low", "close", "volume"]
UTC = timezone.utc


class IndependentAuditError(ValueError):
    pass


def _no_duplicates(pairs):
    doc = {}
    for key, value in pairs:
        if key in doc:
            raise IndependentAuditError(f"Duplicate JSON property: {key}")
        doc[key] = value
    return doc


def _bad_constant(value):
    raise IndependentAuditError(f"Non-JSON number: {value}")


def _json(data: bytes) -> dict:
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates,
                            parse_constant=_bad_constant)
    except (UnicodeError, ValueError) as exc:
        raise IndependentAuditError("Malformed, nonfinite or ambiguous source JSON") from exc
    if not isinstance(result, dict):
        raise IndependentAuditError("Source payload must be a JSON object")
    return result


def _utc_seconds(value: str) -> int:
    if not isinstance(value, str):
        raise IndependentAuditError("UTC timestamp must be a string")
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise IndependentAuditError("Invalid source timestamp") from exc
    if (dt.tzinfo is None or dt.utcoffset() is None or
        dt.utcoffset().total_seconds() != 0 or dt.microsecond != 0):
        raise IndependentAuditError("Source horizon must be an exact UTC second")
    return int(dt.timestamp())


def _bounded_file(root: Path, relative: str, *, max_bytes=2_000_000) -> bytes:
    if (not isinstance(relative, str) or relative.startswith("/") or
        "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise IndependentAuditError("Unsafe archive path")
    p = root / relative
    if p.is_symlink() or not p.resolve().is_relative_to(root):
        raise IndependentAuditError("Archive path escapes root or uses symlink")
    try:
        with p.open("rb") as fp:
            raw = fp.read(max_bytes + 1)
    except OSError as exc:
        raise IndependentAuditError(f"Missing archive file: {relative}") from exc
    if len(raw) > max_bytes:
        raise IndependentAuditError("Archive file exceeds independent audit cap")
    return raw


def _number(s: object, *, allow_zero=False) -> Decimal:
    if not isinstance(s, str) or not s.strip():
        raise IndependentAuditError("OHLCV must contain decimal strings")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise IndependentAuditError("Invalid OHLCV decimal") from exc
    if not d.is_finite() or d < 0 or (d == 0 and not allow_zero):
        raise IndependentAuditError("Nonfinite/negative/zero OHLCV")
    return d


def audit_bundle_independently(directory: Path) -> dict:
    """Verify both archived raw page series separately from primary parser.

    Strict rows and hashes; result is integrity evidence, not source authenticity.
    """
    root = Path(directory).resolve()
    details = {}
    cohort = set()
    digest_parts = []
    for product, stem in PRODUCTS.items():
        source_bytes = _bounded_file(root, f"{stem}.source.json")
        source = _json(source_bytes)
        if (source.get("product") != product or source.get("market") != "SPOT" or
            source.get("granularity") != "FIVE_MINUTE" or
            source.get("symbol") != product.split("-")[0] + "_COINBASE_SPOT" or
            source.get("point_in_time_provenance") != "NOT_ESTABLISHED" or
            source.get("available_at", "MISSING") is not None):
            raise IndependentAuditError("Source identity or provenance mismatch")
        start = _utc_seconds(source.get("start_inclusive_utc"))
        end = _utc_seconds(source.get("end_exclusive_utc"))
        if start >= end or start % 300 or end % 300:
            raise IndependentAuditError("Invalid aligned UTC horizon")
        cohort.add((start, end))
        pages = source.get("pages")
        if not isinstance(pages, list) or not pages:
            raise IndependentAuditError("Missing pages")
        rows = {}
        page_ranges = []
        for i, page in enumerate(pages):
            if not isinstance(page, dict):
                raise IndependentAuditError("Bad page metadata")
            a, b = page.get("from_sec"), page.get("to_sec")
            if type(a) is not int or type(b) is not int or not start <= a < b <= end:
                raise IndependentAuditError("Invalid source page bounds")
            if ("http_request_end_sec_inclusive" in page and
                page["http_request_end_sec_inclusive"] != b - 1):
                raise IndependentAuditError("Raw HTTP request end does not agree with UTC grid")
            page_ranges.append((a, b))
            expected_file = f"raw/{stem}/{i:04d}.json"
            if page.get("raw_file") != expected_file:
                raise IndependentAuditError("Page filename/order inconsistent")
            raw = _bounded_file(root, expected_file)
            actual_digest = hashlib.sha256(raw).hexdigest()
            if actual_digest != page.get("raw_response_sha256") or len(raw) != page.get("raw_response_bytes"):
                raise IndependentAuditError("Raw bytes differ from source metadata")
            candles = _json(raw).get("candles")
            if not isinstance(candles, list) or len(candles) != page.get("count"):
                raise IndependentAuditError("Raw response missing candle array or wrong count")
            for candle in candles:
                if not isinstance(candle, dict):
                    raise IndependentAuditError("Non-object candle")
                t = candle.get("start")
                if not isinstance(t, str) or not re.fullmatch(r"\d{9,12}", t):
                    raise IndependentAuditError("Invalid candle start")
                stamp = int(t)
                if stamp % 300 or stamp < a or stamp >= b or stamp in rows:
                    raise IndependentAuditError("Misaligned, out-of-range or repeated candle")
                values = {field: _number(candle.get(field), allow_zero=(field == "volume"))
                          for field in ROW_NAMES[1:]}
                if values["low"] > min(values["open"], values["close"]) or \
                   values["high"] < max(values["open"], values["close"]) or \
                   values["low"] > values["high"]:
                    raise IndependentAuditError("Bad OHLC envelope")
                rows[stamp] = {field: candle[field] for field in ROW_NAMES[1:]}
            digest_parts.append(f"{product}:{i}:{actual_digest}\n")
        expected_ranges = [(a, min(a + 300 * 300, end)) for a in range(start, end, 300 * 300)]
        if page_ranges != expected_ranges:
            raise IndependentAuditError("Page grid missing, extra or out of order")
        grid = range(start, end, 300)
        if len(rows) != len(grid) or any(t not in rows for t in grid):
            raise IndependentAuditError("Incomplete five-minute grid")
        if (source.get("rows") != len(grid) or source.get("expected_rows") != len(grid) or
            source.get("derived_csv_filename") != stem + ".csv"):
            raise IndependentAuditError("Derived filename or row-count claim mismatch")
        csv_bytes = _bounded_file(root, stem + ".csv", max_bytes=25 * 1024 * 1024)
        actual_csv_sha = hashlib.sha256(csv_bytes).hexdigest()
        if actual_csv_sha != source.get("derived_csv_sha256"):
            raise IndependentAuditError("Derived CSV hash mismatch")
        try:
            decoded = csv_bytes.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
            if reader.fieldnames != ROW_NAMES:
                raise IndependentAuditError("Derived CSV schema mismatch")
            parsed = list(reader)
        except (csv.Error, UnicodeError) as exc:
            raise IndependentAuditError("Invalid derived CSV") from exc
        if len(parsed) != len(grid):
            raise IndependentAuditError("Derived CSV count mismatch")
        for i, stamp in enumerate(grid):
            rec = parsed[i]
            if set(rec) != set(ROW_NAMES) or any(rec[k] is None for k in ROW_NAMES):
                raise IndependentAuditError("Malformed derived CSV fields")
            if rec["time"] != str(stamp * 1000):
                raise IndependentAuditError("Derived candle time/order differs")
            if any(rec[k] != rows[stamp][k] for k in ROW_NAMES[1:]):
                raise IndependentAuditError("Derived CSV not faithful to raw response")
        details[product] = {"rows": len(grid), "start_unix_seconds": start,
                            "end_unix_seconds_exclusive": end, "csv_sha256": actual_csv_sha,
                            "raw_pages": len(pages)}
    if len(cohort) != 1:
        raise IndependentAuditError("BTC and ETH cohort mismatch")
    return {"status": "INTERNAL_CONSISTENCY_PASSED", "markets": details,
            "archive_pages_sha256": hashlib.sha256("".join(digest_parts).encode()).hexdigest(),
            "source_authenticity_proven": False,
            "historical_point_in_time_proven": False,
            "independent_exchange_comparison_proven": False,
            "executable_alpha_proven": False}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    print(json.dumps(audit_bundle_independently(args.directory), indent=2, sort_keys=True))
