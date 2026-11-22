"""Opt-in, read-only Coinbase public 5-minute SPOT data acquisition.

Important: fetching historical exchange OHLCV today does NOT provide the
point-in-time receive timestamps of the historical observations. We intentionally
write NO available_at column and label the dataset unverified.

Stdlib only. No API key, no order endpoints, no auto-running on dashboard startup.
Use the official Coinbase Advanced Trade public candles API.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import bridge

API_ORIGIN = "https://api.coinbase.com"
API_ROUTE = "/api/v3/brokerage/market/products/{product}/candles"
PRODUCTS = {
    "BTC-USD": ("BTC_COINBASE_SPOT", "Bitcoin / USD (Coinbase spot)"),
    "ETH-USD": ("ETH_COINBASE_SPOT", "Ether / USD (Coinbase spot)"),
}
STEP = 300
PAGE_BARS = 300  # Official API max is 350; smaller deterministic pages.
MAX_BARS = 14 * 24 * 12
UTC = UTC


class SourceError(bridge.InputError):
    pass


def utc_ms(value: str) -> int:
    return bridge._time_ms(value)


def _price(value: object, field: str, *, zero_ok: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceError(f"Coinbase {field} must be an explicit decimal string")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise SourceError(f"Coinbase {field} is invalid") from exc
    if not d.is_finite() or (d < 0 if zero_ok else d <= 0):
        raise SourceError(
            f"Coinbase {field} must be finite and positive (volume may be zero)"
        )
    return value


RETRYABLE_HTTP = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_SLEEP_SEC = 5.0


def strict_source_json(raw: bytes) -> dict:
    """Parse original response bytes without silently overwriting JSON object keys.

    JSON with duplicate keys is ambiguous evidence even when the digest matches.
    Coinbase candle payloads and archived sidecars must have one unambiguous value
    for each key. NaN/Infinity are forbidden even though Python json accepts them.
    """

    def reject_duplicate(pairs: list[tuple[str, object]]) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise SourceError(f"Ambiguous duplicate JSON key: {key}")
            obj[key] = value
        return obj

    def reject_constant(value: str) -> None:
        raise SourceError(f"Invalid JSON constant: {value}")

    try:
        result = json.loads(
            raw, object_pairs_hook=reject_duplicate, parse_constant=reject_constant
        )
    except SourceError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise SourceError(
            "Source is not JSON (strict parser rejected invalid or ambiguous values)"
        ) from exc
    if not isinstance(result, dict):
        raise SourceError("Source JSON must be an object")
    return result


def request_json(product: str, start_sec: int, end_sec: int) -> tuple[bytes, dict]:
    """Retry bounded transient HTTP failures; retain exact successful bytes.

    No retry for permanent 4xx responses, malformed JSON or invalid candles.
    The collector remains opt-in and read-only.
    """
    if product not in PRODUCTS:
        raise SourceError("Only public Coinbase spot BTC-USD and ETH-USD are allowed")
    # Observed Coinbase public response on 2026-11-08 includes the candle
    # whose open == the supplied `end` timestamp. Our internal source window
    # is [start_sec, end_sec), so request the last second *inside* that window.
    # This preserves every 5m bar at start < end_sec without hiding spillover.
    params = urlencode(
        {
            "start": start_sec,
            "end": end_sec - 1,
            "granularity": "FIVE_MINUTE",
            "limit": PAGE_BARS,
        }
    )
    url = API_ORIGIN + API_ROUTE.format(product=product) + "?" + params
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "auditable-quant-research-bridge/0.2",
        },
    )
    data = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urlopen(request, timeout=15) as response:
                # Real urllib HTTPS responses expose final URL, status and
                # content type. Never silently accept redirected evidence
                # from another endpoint as a Coinbase market-data response.
                if hasattr(response, "geturl") and response.geturl() != url:
                    raise SourceError(
                        "Coinbase request redirected to a different source URL"
                    )
                if getattr(response, "status", 200) != 200:
                    raise SourceError("Coinbase response does not have HTTP 200 status")
                headers = getattr(response, "headers", None)
                if headers is not None and hasattr(headers, "get_content_type"):
                    if headers.get_content_type() != "application/json":
                        raise SourceError(
                            "Coinbase response has unexpected content type"
                        )
                data = response.read(2_000_001)
            break
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP or attempt == MAX_ATTEMPTS - 1:
                raise SourceError(
                    f"Coinbase HTTP {exc.code}; no further retries"
                ) from exc
            retry_header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                backoff = float(retry_header) if retry_header else float(2**attempt)
            except (ValueError, OverflowError):
                backoff = float(2**attempt)
            if not 0 <= backoff <= MAX_RETRY_SLEEP_SEC:
                backoff = MAX_RETRY_SLEEP_SEC
            time.sleep(backoff)
        except (URLError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise SourceError(
                    "Coinbase connection failed after bounded retries"
                ) from exc
            time.sleep(float(2**attempt))
    if data is None:
        raise SourceError("Coinbase request returned no response")
    if len(data) > 2_000_000:
        raise SourceError("Coinbase payload too large")
    doc = strict_source_json(data)
    return data, doc


def parse_page(doc: dict, *, start_sec: int, end_sec: int) -> dict[int, dict[str, str]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("candles"), list):
        raise SourceError(
            
                'Coinbase did not return a ca'
                'ndles array; API error or in'
                'compatible response'
            
        )
    results: dict[int, dict[str, str]] = {}
    for item in doc["candles"]:
        if not isinstance(item, dict):
            raise SourceError("Candle row must be a JSON object")
        raw_start = item.get("start")
        if not isinstance(raw_start, str) or not re.fullmatch(r"\d{9,12}", raw_start):
            raise SourceError("Candle start must be Unix seconds string")
        t = int(raw_start)
        if t % STEP or not start_sec <= t < end_sec:
            raise SourceError(
                f"Coinbase returned out-of-range or misaligned candle {t}"
            )
        candle = {
            k: _price(item.get(k), k, zero_ok=(k == "volume"))
            for k in ("open", "high", "low", "close", "volume")
        }
        o, hi, lo, cl = (Decimal(candle[k]) for k in ("open", "high", "low", "close"))
        if lo > min(o, cl) or hi < max(o, cl) or lo > hi:
            raise SourceError(f"Invalid OHLC envelope at {t}")
        if t in results and results[t] != candle:
            raise SourceError(f"Conflicting duplicate candle {t}")
        results[t] = candle
    return results


def collect(
    product: str,
    start_ms: int,
    end_ms: int,
    fetch: Callable[[str, int, int], tuple[bytes, dict]] = request_json,
    *,
    pace_sec: float = 0,
    raw_archive: list[tuple[int, int, bytes]] | None = None,
) -> tuple[list[dict], dict]:
    if product not in PRODUCTS:
        raise SourceError("Unsupported product")
    if start_ms % (STEP * 1000) or end_ms % (STEP * 1000):
        raise SourceError("Start and end must align to 5-minute UTC boundaries")
    if end_ms <= start_ms or (end_ms - start_ms) // (STEP * 1000) > MAX_BARS:
        raise SourceError("Requested range must be positive and no more than 14 days")
    # Prevent collecting an unfinished candle; not evidence of past time availability.
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if end_ms > now_ms - 60_000:
        raise SourceError("End must precede present time by at least one minute")
    start_sec, end_sec = start_ms // 1000, end_ms // 1000
    bars: dict[int, dict[str, str]] = {}
    pages: list[dict] = []
    for a in range(start_sec, end_sec, PAGE_BARS * STEP):
        b = min(a + PAGE_BARS * STEP, end_sec)
        raw, doc = fetch(product, a, b)
        if not isinstance(raw, bytes):
            raise SourceError("Coinbase raw HTTP payload must be exact bytes")
        if len(raw) > 2_000_000:
            raise SourceError("Coinbase raw HTTP payload exceeds safe per-page size")
        # The parsed rows and the hashed original response must be the SAME source.
        verified_doc = strict_source_json(raw)
        if verified_doc != doc:
            raise SourceError("Raw Coinbase response and parsed response disagree")
        page = parse_page(verified_doc, start_sec=a, end_sec=b)
        # Local ingest-clock observation (after HTTP receipt), NOT the
        # historical time this completed candle was available to a trader.
        received_locally_at_utc = datetime.now(UTC).isoformat()
        if raw_archive is not None:
            raw_archive.append((a, b, raw))
        for t, row in page.items():
            if t in bars and bars[t] != row:
                raise SourceError("Conflicting cross-page duplicate candle")
            bars[t] = row
        pages.append(
            {
                "from_sec": a,
                "to_sec": b,
                "http_request_end_sec_inclusive": b - 1,
                "local_ingest_clock_utc": received_locally_at_utc,
                "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_response_bytes": len(raw),
                "count": len(page),
            }
        )
        if pace_sec:
            time.sleep(pace_sec)
    expected = set(range(start_sec, end_sec, STEP))
    observed = set(bars)
    gaps = sorted(expected - observed)
    if gaps:
        raise SourceError(
            f"Missing {len(gaps)} five-minute candles in Coinbase responses; first={gaps[0]}"
        )
    ordered = [{"time": str(t * 1000), **bars[t]} for t in sorted(bars)]
    manifest = {
        "source": "Coinbase Advanced Trade public REST candles",
        "url": API_ORIGIN + API_ROUTE.format(product=product),
        "market": "SPOT",
        "product": product,
        "granularity": "FIVE_MINUTE",
        "symbol": PRODUCTS[product][0],
        "start_inclusive_utc": datetime.fromtimestamp(start_sec, UTC).isoformat(),
        "end_exclusive_utc": datetime.fromtimestamp(end_sec, UTC).isoformat(),
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "rows": len(ordered),
        "expected_rows": len(expected),
        "pages": pages,
        "raw_payload_bytes_released": False,
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
            'establish historical event r'
            'eceipt time or executable sl'
            'ippage.'
        ),
    }
    return ordered, manifest


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".coinbase-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def export(
    product: str,
    start_ms: int,
    end_ms: int,
    out_dir: Path,
    fetch: Callable[[str, int, int], tuple[bytes, dict]] = request_json,
    *,
    register: bool = False,
    pace_sec: float = 0.0,
    manifest_path: Path | None = None,
) -> tuple[Path, Path]:
    candles, source = collect(product, start_ms, end_ms, fetch, pace_sec=pace_sec)
    out_dir = out_dir.resolve()
    if register and out_dir != bridge.DATA_ROOT:
        raise SourceError(
            "Can only register files placed in the bridge backend/data folder"
        )
    if register:
        # The one-product exporter stores source hashes but not raw HTTP pages,
        # so it cannot meet the paired evidence gate required for registration.
        raise SourceError(
            
                'Single-product registration '
                'is disabled; use fetch_btc_e'
                'th.py for audited paired arc'
                'hives'
            
        )
    base = PRODUCTS[product][0].lower() + "_5m"
    path = out_dir / f"{base}.csv"
    sidecar = out_dir / f"{base}.source.json"
    # Fail on preexisting files to avoid silently overwriting a previously
    # audited snapshot.
    if path.exists() or sidecar.exists():
        raise SourceError(
            
                'Output exists. Preserve old '
                'source; choose a new output '
                'directory or archive existin'
                'g data first.'
            
        )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=["time", "open", "high", "low", "close", "volume"],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(candles)
    payload = buffer.getvalue().encode("utf-8")
    source["derived_csv_sha256"] = hashlib.sha256(payload).hexdigest()
    source["derived_csv_filename"] = path.name
    # Check complete derived data schema before committing output.
    verified = []
    for bar in candles:
        verified.append(
            {
                "time": bridge._time_ms(bar["time"]),
                **{
                    k: float(bar[k]) for k in ["open", "high", "low", "close", "volume"]
                },
            }
        )
    if len(verified) != len(candles):
        raise AssertionError("Internal validation failed")
    manifest = (manifest_path or bridge.MANIFEST).resolve()
    updated = None
    if register:
        previous = bridge.read_manifest(manifest)
        updated = json.loads(json.dumps(previous))
        if any(
            x.get("symbol") == PRODUCTS[product][0] and x.get("resolution") == "5"
            for x in updated["datasets"]
        ):
            raise SourceError("Symbol/resolution already registered")
        updated["datasets"].append(
            {
                "symbol": PRODUCTS[product][0],
                "name": PRODUCTS[product][1],
                "exchange": "Coinbase Advanced Trade",
                "timezone": "UTC",
                "session": "24x7",
                "type": "crypto",
                "resolution": "5",
                "file": path.name,
                "source_type": "real",
                "evidence_status": "unverified",
                "pricePrecision": 2,
                "tickSize": 0.01,
            }
        )
    _write_atomic(path, payload)
    _write_atomic(
        sidecar, (json.dumps(source, indent=2, sort_keys=True) + "\n").encode()
    )
    if updated is not None:
        # The original research repo is never touched. Only bridge manifest is updated.
        _write_atomic(
            manifest, (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
        )
    return path, sidecar


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=sorted(PRODUCTS), required=True)
    parser.add_argument(
        "--start", required=True, help="UTC timestamp aligned to 5 min (inclusive)"
    )
    parser.add_argument(
        "--end", required=True, help="UTC timestamp aligned to 5 min (exclusive)"
    )
    parser.add_argument("--output-dir", type=Path, default=bridge.DATA_ROOT)
    parser.add_argument(
        "--register", action="store_true", help="Add to read-only chart manifest"
    )
    args = parser.parse_args()
    try:
        csv_path, source_path = export(
            args.product,
            utc_ms(args.start),
            utc_ms(args.end),
            args.output_dir,
            register=args.register,
            pace_sec=0.25,
        )
    except bridge.InputError as exc:
        parser.error(str(exc))
    print("Coinbase public historical data saved:", csv_path)
    print("Source/provenance (NOT point-in-time verified):", source_path)
    print("No trading, no authentication, no alpha claim. No available_at invented.")


if __name__ == "__main__":
    main()
