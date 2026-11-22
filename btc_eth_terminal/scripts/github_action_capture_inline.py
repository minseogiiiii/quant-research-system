"""Only for isolated network-enabled GitHub Actions evidence capture.
Do not run for alpha; this script creates an unsigned, read-only source archive.
"""

import csv
import hashlib
import io
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UTC = UTC
start = int(datetime(2026, 11, 8, tzinfo=UTC).timestamp())
end = start + 86400
root = Path("evidence")
root.mkdir(exist_ok=True)
results = {
    "status": "INCOMPLETE",
    "requested_range_utc": [start, end],
    "products": {},
    "point_in_time_verified": False,
    "executable_alpha_verified": False,
    "orders_enabled": False,
}


def pairs_no_dup(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate source JSON key")
        obj[key] = value
    return obj


def not_json_constant(token):
    raise ValueError("non-JSON number")


def price(raw, allow_zero=False):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("non-string OHLCV")
    v = Decimal(raw)
    if not v.is_finite() or v < 0 or (not allow_zero and v == 0):
        raise ValueError("nonfinite/negative OHLCV")
    return v


try:
    for product in ("BTC-USD", "ETH-USD"):
        slug = product.split("-")[0].lower() + "_coinbase_spot_5m"
        relative_url = "/api/v3/brokerage/market/products/" + product + "/candles"
        params = urlencode(
            {"start": start, "end": end - 1, "granularity": "FIVE_MINUTE", "limit": 300}
        )
        url = "https://api.coinbase.com" + relative_url + "?" + params
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "btc-eth-evidence-capture/0.1",
            },
        )
        with urlopen(request, timeout=25) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("HTTP status or redirected source URL")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Non-JSON response content type")
            raw = response.read(2_000_001)
        received_at = datetime.now(UTC).isoformat()
        if len(raw) > 2_000_000:
            raise ValueError("Payload exceeds limit")
        raw_dir = root / "raw" / slug
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "0000.json").write_bytes(raw)
        decoded = json.loads(
            raw, object_pairs_hook=pairs_no_dup, parse_constant=not_json_constant
        )
        candles = decoded.get("candles") if isinstance(decoded, dict) else None
        if not isinstance(candles, list):
            raise ValueError("Candles array missing")
        by_stamp = {}
        for c in candles:
            stamp = c.get("start")
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{9,12}", stamp):
                raise ValueError("Unexpected candle timestamp type")
            t = int(stamp)
            if t % 300 or not start <= t < end or t in by_stamp:
                raise ValueError("Duplicate, misaligned or out-of-range candle")
            prices = {
                k: price(c.get(k), allow_zero=(k == "volume"))
                for k in ("open", "high", "low", "close", "volume")
            }
            if (
                prices["low"] > min(prices["open"], prices["close"])
                or prices["high"] < max(prices["open"], prices["close"])
                or prices["low"] > prices["high"]
            ):
                raise ValueError("Bad OHLC envelope")
            by_stamp[t] = c
        expected = list(range(start, end, 300))
        if len(by_stamp) != len(expected) or any(t not in by_stamp for t in expected):
            raise ValueError("Incomplete historical candle grid")
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for t in expected:
            c = by_stamp[t]
            writer.writerow(
                [
                    str(t * 1000),
                    *(c[k] for k in ("open", "high", "low", "close", "volume")),
                ]
            )
        csv_bytes = buf.getvalue().encode("utf-8")
        (root / f"{slug}.csv").write_bytes(csv_bytes)
        sha = hashlib.sha256(csv_bytes).hexdigest()
        source = {
            "source": "Coinbase Advanced Trade public REST candles",
            "url": "https://api.coinbase.com" + relative_url,
            "market": "SPOT",
            "product": product,
            "granularity": "FIVE_MINUTE",
            "symbol": product.split("-")[0] + "_COINBASE_SPOT",
            "start_inclusive_utc": datetime.fromtimestamp(start, UTC).isoformat(),
            "end_exclusive_utc": datetime.fromtimestamp(end, UTC).isoformat(),
            "retrieved_at_utc": received_at,
            "rows": len(expected),
            "expected_rows": len(expected),
            "pages": [
                {
                    "from_sec": start,
                    "to_sec": end,
                    "http_request_end_sec_inclusive": end - 1,
                    "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_response_bytes": len(raw),
                    "count": len(candles),
                    "raw_file": f"raw/{slug}/0000.json",
                    "local_ingest_clock_utc": received_at,
                }
            ],
            "raw_payload_bytes_released": True,
            "event_time_equals_bar_open": True,
            "available_at": None,
            "point_in_time_provenance": "NOT_ESTABLISHED",
            "evidence_status": "unverified",
            "no_automatic_alpha_claim": True,
            "price_unit": "USD per base asset",
            "volume_unit": "base asset (as specified by exchange)",
            "no_trading_or_authentication": True,
            "data_caveat": (
                'Historical snapshots cannot '
                'prove historical market-data'
                ' arrival or executable fills'
                '.'
            ),
            "derived_csv_filename": f"{slug}.csv",
            "derived_csv_sha256": sha,
        }
        (root / f"{slug}.source.json").write_text(
            json.dumps(source, indent=2, sort_keys=True) + "\n"
        )
        results["products"][product] = {
            "count": len(expected),
            "csv_sha256": sha,
            "original_response_sha256": hashlib.sha256(raw).hexdigest(),
            "original_response_size": len(raw),
        }
    results["status"] = "TWO_PRODUCT_CAPTURE_COMPLETE_NOT_PIT_PROVEN"
except Exception as exc:
    results["status"] = "FAILED_INCOMPLETE_OR_UNVERIFIED"
    results["error_type"] = type(exc).__name__
    results["error"] = str(exc)[:500]
finally:
    results["finished_at_utc"] = datetime.now(UTC).isoformat()
    (root / "capture_status.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(results, indent=2, sort_keys=True))
    if results["status"] != "TWO_PRODUCT_CAPTURE_COMPLETE_NOT_PIT_PROVEN":
        sys.exit(1)
