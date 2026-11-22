"""Independent Decimal OHLCV reference from a genuine SHA-admitted Coinbase archive.

The reference deliberately does not call the v36 engine's candle aggregation
logic. It does reuse Archive Ingress for original byte integrity and the
existing canonical LiveMarketEngine only for the comparator.

No incomplete, unobserved, snapshot-startup or fabricated no-trade bars.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from v35_replay_service import StateRejected
    from v36_live_engine import LiveMarketEngine
    from v36_live_gateway import server_for_live
    from v36_play_verified_archive import admitted_verified_chunks

    return StateRejected, LiveMarketEngine, server_for_live, admitted_verified_chunks


StateRejected, LiveMarketEngine, server_for_live, admitted_verified_chunks = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0

PRODUCTS = ("BTC-USD", "ETH-USD")
KEYS = ("open", "high", "low", "close", "volume")


def utc_ms(stamp: str) -> int:
    if not isinstance(stamp, str) or not stamp.endswith("Z"):
        raise StateRejected("INDEPENDENT_REFERENCE_BAD_RECEIVER_TIME")
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)


def reference_ohlcv(records: list[bytes]) -> dict[str, list[dict]]:
    """Recompute from raw source events, not the derived API or candle engine."""
    bars = {p: {} for p in PRODUCTS}
    last_observation = None
    first_observation = None
    availability = {}
    for line in records:
        envelope = json.loads(line)
        recv = utc_ms(envelope["received_utc"])
        if first_observation is None:
            first_observation = recv
        last_observation = recv
        wire = json.loads(envelope["raw_text"])
        # The first source message whose receipt proves each interval ended.
        bucket = recv // 60_000
        for b in list({i for p in PRODUCTS for i in bars[p]}):
            if (b + 60_000) <= recv and b not in availability:
                availability[b] = recv
        if wire["channel"] != "market_trades":
            continue
        for ev in wire["events"]:
            if ev.get("type") != "update":
                continue
            for trade in ev.get("trades", []):
                p = trade.get("product_id")
                if p not in PRODUCTS:
                    raise StateRejected("UNEXPECTED_TRADE_PRODUCT")
                price = Decimal(trade["price"])
                qty = Decimal(trade["size"])
                if (
                    not price.is_finite()
                    or not qty.is_finite()
                    or price <= 0
                    or qty <= 0
                ):
                    raise StateRejected("INVALID_INDEPENDENT_TRADE")
                opened = recv // 60_000 * 60_000
                x = bars[p].get(opened)
                if x is None:
                    x = {
                        "time": opened,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": qty,
                        "observed_trade_count": 1,
                    }
                    bars[p][opened] = x
                else:
                    x["high"] = max(x["high"], price)
                    x["low"] = min(x["low"], price)
                    x["close"] = price
                    x["volume"] += qty
                    x["observed_trade_count"] += 1
    if first_observation is None:
        raise StateRejected("EMPTY_REFERENCE")
    earliest = (first_observation + 59999) // 60000 * 60000
    output = {}
    for p in PRODUCTS:
        output[p] = []
        for t, bar in sorted(bars[p].items()):
            if t < earliest or t + 60000 > last_observation or t not in availability:
                continue
            output[p].append({**bar, "available_at": availability[t]})
    return output


def compare_private_capture(
    capture: Path, pin: str, require_completed: int = 0
) -> dict:
    generator = admitted_verified_chunks(capture, pin)
    certificate, _ = next(generator)
    engine = LiveMarketEngine(mode="ARCHIVE_SIMULATION")
    records = []
    for _, chunk in generator:
        for line in chunk.splitlines(keepends=True):
            records.append(line)
            engine.consume(line)
    if len(records) != certificate["messages"]:
        raise StateRejected("ARCHIVE_MESSAGE_COUNT_MISMATCH")
    reference = reference_ohlcv(records)
    server = server_for_live(engine)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = {}
        for p in PRODUCTS:
            expected = reference[p]
            derived = engine.candles_page(p)["rows"]
            with urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/api/v36/candles?product={p}",
                timeout=10,
            ) as res:
                actual = json.load(res)["rows"]
            if len(expected) != len(actual) or len(expected) != len(derived):
                raise StateRejected("INDEPENDENT_CANDLE_COUNT_MISMATCH_" + p)
            for i, (r, d, a) in enumerate(zip(expected, derived, actual, strict=False)):
                for field in ("time", "available_at", "observed_trade_count"):
                    if r[field] != d[field] or r[field] != a[field]:
                        raise StateRejected(
                            f"INDEPENDENT_CANDLE_{field}_MISMATCH_{p}_{i}"
                        )
                for field in KEYS:
                    if r[field] != Decimal(d[field]) or r[field] != Decimal(a[field]):
                        raise StateRejected(
                            f"INDEPENDENT_CANDLE_{field}_MISMATCH_{p}_{i}"
                        )
            if len(expected) < require_completed:
                raise StateRejected("INSUFFICIENT_COMPLETED_REAL_CANDLES_" + p)
            result[p] = {
                "completed_candle_count": len(expected),
                "first_completed_minute": expected[0]["time"] if expected else None,
                "last_completed_minute": expected[-1]["time"] if expected else None,
                "numeric_ohlcv_and_http_parity": "PASS",
            }
        return {
            "schema": "v36_1_independent_real_trade_candle_audit_v1",
            "mode": "ARCHIVE_SIMULATION_NOT_LIVE",
            "original_source_messages": len(records),
            "capture_manifest_sha256": pin,
            "reference_method": "INDEPENDENT_RAW_TRADE_DECIMAL_BUCKETS",
            "products": result,
            "funnest_actual_getbars_verified": False,
            "browser_live_verified": False,
            "trading_enabled": False,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--min-completed-per-product", type=int, default=0)
    args = parser.parse_args()
    try:
        result = compare_private_capture(
            args.capture, args.manifest_sha256, args.min_completed_per_product
        )
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        OSError,
        InvalidOperation,
        StateRejected,
    ) as exc:
        print("FAIL_INDEPENDENT_COINBASE_OHLCV " + str(exc).splitlines()[0][:250])
        return 2
    print("PASS_INDEPENDENT_COINBASE_OHLCV " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
