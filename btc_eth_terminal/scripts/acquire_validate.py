"""Opt-in real-provider BTC/ETH capture and dual-auditor integrity gate.

Use on a machine with outbound HTTPS. Never auto-trades, assigns profits,
or promotes vendor historical candles into point-in-time execution evidence.
No fake prices, fallback data source, or synthesized candles on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "backend"))


def _q14_load_after_path_setup_0():
    import bridge
    import fetch_btc_eth
    import fetch_coinbase_5m as coinbase
    from independent_audit import audit_bundle_independently

    return bridge, fetch_btc_eth, coinbase, audit_bundle_independently


bridge, fetch_btc_eth, coinbase, audit_bundle_independently = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0


def dns_probe(host: str = "api.coinbase.com") -> dict:
    try:
        addresses = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return {
            "status": "FAILED",
            "host": host,
            "error_type": type(exc).__name__,
            "error": str(exc)[:350],
        }
    return {
        "status": "RESOLVED",
        "host": host,
        "distinct_ip_count": len({x[4][0] for x in addresses}),
    }


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    coinbase._write_atomic(
        path,
        (
            json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8"),
    )


def capture(
    start_ms: int,
    end_ms: int,
    *,
    root: Path | None = None,
    manifest_path: Path | None = None,
    fetch: Callable[[str, int, int], tuple[bytes, dict]] = coinbase.request_json,
    repeat: bool = False,
    register: bool = False,
    run_dns_probe: bool = True,
) -> dict:
    if register and not repeat:
        raise ValueError("--register requires --repeat and two matching captures")
    root = (root or bridge.DATA_ROOT).resolve()
    manifest = (manifest_path or bridge.MANIFEST).resolve()
    started = datetime.now(UTC).isoformat()
    status = {
        "started_at_utc": started,
        "requested_window": {
            "start_ms": start_ms,
            "end_ms_exclusive": end_ms,
            "products": list(fetch_btc_eth.PAIR),
            "granularity_seconds": 300,
        },
        "provider": "Coinbase Advanced Trade public historical candles API",
        "feed_class": "SPOT_OHLCV_HISTORICAL_ONLY",
        "attempt_kind": "REAL_HTTP"
        if fetch is coinbase.request_json
        else "TEST_INJECTED_FETCH",
        "status": "INITIALIZED",
        "source_authenticity_independently_proven": False,
        "historical_point_in_time_proven": False,
        "execution_prices_proven": False,
        "real_trades_or_l2_collected": False,
        "trading_enabled": False,
        "executable_alpha_proven": False,
        "registered": False,
        "raw_market_bytes_saved": False,
    }
    if run_dns_probe:
        status["dns_preflight"] = dns_probe()
    expected_manifest_sha = (
        hashlib.sha256(manifest.read_bytes()).hexdigest() if register else None
    )
    try:
        captured = fetch_btc_eth.export_pair(
            start_ms,
            end_ms,
            data_root=root,
            manifest_path=manifest,
            fetch=fetch,
            register=False,
            pace_sec=0.25 if fetch is coinbase.request_json else 0,
        )
        status["archive_directory"] = str(captured)
        status["raw_market_bytes_saved"] = True
        primary = fetch_btc_eth.verify_bundle(captured)
        independent = audit_bundle_independently(captured)
        status["primary_audit"] = primary
        status["independent_audit"] = independent
        status["status"] = "CAPTURED_AND_DUAL_AUDITED_NOT_PIT"
        if repeat:
            repeated_root = root / "repeat_captures"
            captured_again = fetch_btc_eth.export_pair(
                start_ms,
                end_ms,
                data_root=repeated_root,
                manifest_path=manifest,
                fetch=fetch,
                register=False,
                pace_sec=0.25 if fetch is coinbase.request_json else 0,
            )
            repeat_primary = fetch_btc_eth.verify_bundle(captured_again)
            repeat_independent = audit_bundle_independently(captured_again)
            status["repeat_archive_directory"] = str(captured_again)
            status["repeat_primary_audit"] = repeat_primary
            status["repeat_independent_audit"] = repeat_independent
            # Compare canonical candle sets. Original HTTP byte encodings/order may
            # vary.
            pairs = ["BTC-USD", "ETH-USD"]
            status["same_provider_repeat_match"] = all(
                primary["markets"][p]["sha256"]
                == repeat_primary["markets"][p]["sha256"]
                for p in pairs
            )
            if not status["same_provider_repeat_match"]:
                status["status"] = "REPEAT_DIFFERED_NO_REGISTRATION"
                status["reason"] = (
                    
                        'Repeat historical candle val'
                        'ues changed; review both pre'
                        'served archives'
                    
                )
                return status
        if register:
            fetch_btc_eth.register_verified_bundle(
                captured,
                manifest_path=manifest,
                expected_manifest_sha=expected_manifest_sha,
            )
            status["registered"] = True
            status["status"] = "REGISTERED_UNVERIFIED_HISTORICAL_SPOT_BARS"
        return status
    except (OSError, ValueError, KeyError, TypeError) as exc:
        status["status"] = "CAPTURE_FAILED_UNVERIFIED"
        status["error_type"] = type(exc).__name__
        status["error"] = str(exc)[:600]
        # Preserve enough chained diagnostic detail to distinguish DNS from HTTP
        # failures without claiming an absent source or recording credentials.
        if exc.__cause__ is not None:
            status["root_cause_type"] = type(exc.__cause__).__name__
            status["root_cause"] = str(exc.__cause__)[:450]
        return status
    finally:
        status["completed_at_utc"] = datetime.now(UTC).isoformat()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--start",
        required=True,
        help="UTC 5-minute boundary, e.g. 2026-11-08T00:00:00Z",
    )
    p.add_argument(
        "--end", required=True, help="UTC exclusive boundary, e.g. 2026-11-09T00:00:00Z"
    )
    p.add_argument(
        "--repeat",
        action="store_true",
        help=(
            'Re-download both markets; pr'
            'eserve second raw archive an'
            'd compare candles'
        ),
    )
    p.add_argument(
        "--register",
        action="store_true",
        help=(
            'Opt-in chart registration on'
            'ly after matching repeat and'
            ' independent audit'
        ),
    )
    p.add_argument(
        "--report", type=Path, default=BASE / "reports" / "latest_capture_attempt.json"
    )
    args = p.parse_args()
    try:
        start_ms, end_ms = coinbase.utc_ms(args.start), coinbase.utc_ms(args.end)
        result = capture(start_ms, end_ms, repeat=args.repeat, register=args.register)
    except (ValueError, OSError, TypeError) as exc:
        result = {
            "status": "CAPTURE_NOT_STARTED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "raw_market_bytes_saved": False,
            "registered": False,
            "executable_alpha_proven": False,
        }
    _write_report(args.report, result)
    print(json.dumps(result, sort_keys=True, indent=2))
    print("Audit report:", args.report.resolve())
    if result["status"] not in {
        "CAPTURED_AND_DUAL_AUDITED_NOT_PIT",
        "REGISTERED_UNVERIFIED_HISTORICAL_SPOT_BARS",
    }:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
