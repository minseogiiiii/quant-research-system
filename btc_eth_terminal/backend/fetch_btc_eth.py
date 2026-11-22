"""Atomic, read-only two-market Coinbase historical acquisition for BTC-USD & ETH-USD.

Both markets must be fetched and audited before either is registered. Original
HTTP JSON responses are retained byte-for-byte for offline source-to-chart
reconstruction. Does not create fills, evidence of historical availability, or alpha.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import bridge
import fetch_coinbase_5m as coinbase
from independent_audit import audit_bundle_independently

PAIR = ("BTC-USD", "ETH-USD")


def _safe_run_name(start_ms: int, end_ms: int) -> str:
    if start_ms >= end_ms or start_ms % 300_000 or end_ms % 300_000:
        raise coinbase.SourceError("Both bounds must be UTC-aligned 5-minute instants")
    start = datetime.fromtimestamp(start_ms / 1000, UTC).strftime("%Y%m%dT%H%MZ")
    end = datetime.fromtimestamp(end_ms / 1000, UTC).strftime("%Y%m%dT%H%MZ")
    return f"coinbase_spot_5m_{start}_{end}"


def _csv_bytes(rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["time", "open", "high", "low", "close", "volume"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _atomic_write(path: Path, contents: bytes) -> None:
    coinbase._write_atomic(path, contents)


def verify_bundle(directory: Path) -> dict:
    """Reconstruct BOTH CSV files from retained raw JSON and verify every byte.

    No internet access, no model inputs, no simulated fills. Reject missing
    pages, modified payloads, changed derived files or incomplete bar coverage.
    """
    directory = directory.resolve()
    summary: dict[str, dict] = {}
    horizons: set[tuple[int, int]] = set()
    for product in PAIR:
        stem = coinbase.PRODUCTS[product][0].lower() + "_5m"
        meta_path = directory / f"{stem}.source.json"
        csv_path = directory / f"{stem}.csv"
        if meta_path.is_symlink() or csv_path.is_symlink():
            raise coinbase.SourceError("Source archive cannot contain symlinked metadata or CSV")
        if meta_path.stat().st_size > 2_000_000:
            raise coinbase.SourceError("Source metadata exceeds audit size limit")
        source = coinbase.strict_source_json(meta_path.read_bytes())
        if source.get("product") != product or source.get("granularity") != "FIVE_MINUTE":
            raise coinbase.SourceError("Source metadata instrument or resolution mismatch")
        if (source.get("symbol") != coinbase.PRODUCTS[product][0]
                or source.get("market") != "SPOT"
                or source.get("source") != "Coinbase Advanced Trade public REST candles"
                or source.get("url") != coinbase.API_ORIGIN + coinbase.API_ROUTE.format(product=product)
                or source.get("event_time_equals_bar_open") is not True
                or source.get("point_in_time_provenance") != "NOT_ESTABLISHED"
                or source.get("evidence_status") != "unverified"):
            raise coinbase.SourceError("Source identity/provenance assertions do not match the declared product")
        if source.get("available_at", "MISSING") is not None:
            raise coinbase.SourceError("Historical data must not claim observed historical availability")
        start_time = datetime.fromisoformat(source["start_inclusive_utc"])
        end_time = datetime.fromisoformat(source["end_exclusive_utc"])
        if (start_time.utcoffset() is None or end_time.utcoffset() is None
                or start_time.utcoffset().total_seconds() != 0
                or end_time.utcoffset().total_seconds() != 0):
            raise coinbase.SourceError("Source horizon must explicitly use UTC")
        if start_time.microsecond or end_time.microsecond:
            raise coinbase.SourceError("Source horizon must use exact second boundaries")
        start_sec = int(start_time.timestamp())
        end_sec = int(end_time.timestamp())
        horizons.add((start_sec, end_sec))
        try:
            retrieved = datetime.fromisoformat(source["retrieved_at_utc"])
        except (TypeError, KeyError, ValueError) as exc:
            raise coinbase.SourceError("Source retrieval timestamp is missing or invalid") from exc
        if (retrieved.utcoffset() is None or retrieved.utcoffset().total_seconds() != 0
                or retrieved.timestamp() < end_sec
                or retrieved.timestamp() > datetime.now(UTC).timestamp() + 60):
            raise coinbase.SourceError("Source retrieval timestamp must be UTC, after candle completion, and not future-dated")
        if start_sec % 300 or end_sec % 300 or end_sec <= start_sec:
            raise coinbase.SourceError("Invalid page horizon in source metadata")
        expected = set(range(start_sec, end_sec, 300))
        pages = source.get("pages")
        if not isinstance(pages, list) or not pages:
            raise coinbase.SourceError("Missing archived raw pages")
        gathered: dict[int, dict] = {}
        seen_intervals: list[tuple[int, int]] = []
        for i, page in enumerate(pages):
            # New captures record a local software receive-clock timestamp.
            # Existing v6 archives may lack it, never infer it retroactively.
            ingest_clock = page.get("local_ingest_clock_utc") if isinstance(page, dict) else None
            if ingest_clock is not None:
                try:
                    observed_at = datetime.fromisoformat(ingest_clock)
                    if (observed_at.utcoffset() is None or
                        observed_at.utcoffset().total_seconds() != 0 or
                        not end_sec <= observed_at.timestamp() <= retrieved.timestamp()):
                        raise ValueError("Ingest time outside observed collection window")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise coinbase.SourceError("Invalid local collection clock metadata") from exc
            if not isinstance(page, dict) or page.get("raw_file") != f"raw/{stem}/{i:04d}.json":
                raise coinbase.SourceError("Unexpected raw page filename or page order")
            a, b = int(page["from_sec"]), int(page["to_sec"])
            if ("http_request_end_sec_inclusive" in page and
                page["http_request_end_sec_inclusive"] != b - 1):
                raise coinbase.SourceError("Archived HTTP request end is inconsistent with exclusive candle horizon")
            if not start_sec <= a < b <= end_sec or (b - a) > coinbase.PAGE_BARS * 300:
                raise coinbase.SourceError("Bad page boundary")
            raw_path = directory / page["raw_file"]
            if (not raw_path.resolve().is_relative_to(directory)
                    or raw_path.is_symlink() or raw_path.stat().st_size > 2_000_000):
                raise coinbase.SourceError("Raw HTTP page must be a bounded in-archive file")
            raw = raw_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != page.get("raw_response_sha256"):
                raise coinbase.SourceError("Archived raw HTTP page SHA-256 mismatch")
            if len(raw) != page.get("raw_response_bytes"):
                raise coinbase.SourceError("Archived raw HTTP response length mismatch")
            page_rows = coinbase.parse_page(coinbase.strict_source_json(raw), start_sec=a, end_sec=b)
            if len(page_rows) != page.get("count"):
                raise coinbase.SourceError("Archived page row count mismatch")
            for timestamp, row in page_rows.items():
                if timestamp in gathered and gathered[timestamp] != row:
                    raise coinbase.SourceError("Conflicting raw candles between pages")
                gathered[timestamp] = row
            seen_intervals.append((a, b))
        if seen_intervals != [
            (a, min(a + coinbase.PAGE_BARS * 300, end_sec))
            for a in range(start_sec, end_sec, coinbase.PAGE_BARS * 300)
        ]:
            raise coinbase.SourceError("Missing, overlapping or reordered archive pages")
        if set(gathered) != expected:
            raise coinbase.SourceError("Archive source is incomplete")
        reconstructed = [{"time": str(t * 1000), **gathered[t]} for t in sorted(gathered)]
        expected_csv = _csv_bytes(reconstructed)
        actual_csv = csv_path.read_bytes()
        if actual_csv != expected_csv or hashlib.sha256(actual_csv).hexdigest() != source.get("derived_csv_sha256"):
            raise coinbase.SourceError("CSV does not match archived original Coinbase pages")
        if len(reconstructed) != source.get("rows") or len(expected) != source.get("expected_rows"):
            raise coinbase.SourceError("Archived row-count claim mismatch")
        summary[product] = {"rows": len(expected), "sha256": hashlib.sha256(actual_csv).hexdigest()}
    if len(horizons) != 1:
        raise coinbase.SourceError("BTC/ETH paired archives have mismatched time ranges")
    return {"verified": True, "markets": summary, "execution": False, "point_in_time_proven": False}



def register_verified_bundle(
    directory: Path,
    *,
    manifest_path: Path | None = None,
    expected_manifest_sha: str | None = None,
) -> None:
    """Register a previously archived pair under an OS advisory lock.

    Both independent source auditors must pass *while holding the lock*;
    never publish an unverified chart, nor erase another writer's manifest.
    The lock works on Unix/macOS; it is intentionally not a web service.
    """
    directory = Path(directory).resolve()
    root = bridge.DATA_ROOT.resolve()
    manifest = (manifest_path or bridge.MANIFEST).resolve()
    if directory.parent != root or directory.is_symlink():
        raise coinbase.SourceError("Only a bundle directly under backend/data may be registered")
    lockpath = manifest.with_name(".btc_eth_manifest.lock")
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open("a+b") as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            current_raw = manifest.read_bytes()
            if (expected_manifest_sha is not None and
                hashlib.sha256(current_raw).hexdigest() != expected_manifest_sha):
                raise coinbase.SourceError("Manifest changed since acquisition started")
            current = bridge.read_manifest(manifest)
            if current.get("scope") != "BTC_ETH_SPOT_5M_RESEARCH_ONLY":
                raise coinbase.SourceError("Refuse registration outside BTC/ETH pilot scope")
            if any(ds.get("symbol") in {coinbase.PRODUCTS[p][0] for p in PAIR}
                   for ds in current["datasets"]):
                raise coinbase.SourceError("BTC or ETH already registered: preserve earlier run")
            verify_bundle(directory)
            audit_bundle_independently(directory)
            updated = json.loads(json.dumps(current))
            for product in PAIR:
                symbol, name = coinbase.PRODUCTS[product]
                stem = symbol.lower() + "_5m"
                updated["datasets"].append({
                    "symbol": symbol, "name": name, "exchange": "Coinbase Advanced Trade",
                    "timezone": "UTC", "session": "24x7", "type": "crypto",
                    "resolution": "5", "file": f"{directory.name}/{stem}.csv",
                    "source_type": "real", "evidence_status": "unverified",
                    "pricePrecision": 2, "tickSize": 0.01,
                    "chart_tick_is_display_only": True,
                    "provenance_file": f"{directory.name}/{stem}.source.json",
                })
            # Independently verify the exact files and binding before writing.
            for dataset in updated["datasets"][-2:]:
                bridge.parse_bars(dataset["file"], completed_bar_ms=300_000)
                bridge._registered_bundle_directory(dataset)
            _atomic_write(manifest, (json.dumps(updated, sort_keys=True, indent=2) + "\n").encode())
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def export_pair(
    start_ms: int,
    end_ms: int,
    *,
    data_root: Path | None = None,
    manifest_path: Path | None = None,
    fetch: Callable[[str, int, int], tuple[bytes, dict]] = coinbase.request_json,
    register: bool = False,
    pace_sec: float = 0,
) -> Path:
    """Commit source-checked BOTH markets to one run dir; register only afterward."""
    root = (data_root or bridge.DATA_ROOT).resolve()
    manifest_path = (manifest_path or bridge.MANIFEST).resolve()
    run_name = _safe_run_name(start_ms, end_ms)
    final = root / run_name
    # Validate all obvious policy, path, and manifest issues BEFORE network work.
    if register and root != bridge.DATA_ROOT.resolve():
        raise coinbase.SourceError("Registered data must be under backend/data")
    if final.exists():
        raise coinbase.SourceError("Run already exists: immutable source directory cannot be overwritten")
    previous = bridge.read_manifest(manifest_path) if register else None
    manifest_sha_at_start = (hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                             if previous is not None else None)
    if previous is not None:
        for product in PAIR:
            symbol = coinbase.PRODUCTS[product][0]
            if any(ds.get("symbol") == symbol and ds.get("resolution") == "5" for ds in previous["datasets"]):
                raise coinbase.SourceError("BTC/ETH market already registered; preserve existing run")
    # No disk writes until BOTH markets have been fetched and validated.
    collected: dict[str, tuple[list[dict], dict, list[tuple[int, int, bytes]]]] = {}
    for product in PAIR:
        raw_pages: list[tuple[int, int, bytes]] = []
        rows, source = coinbase.collect(product, start_ms, end_ms, fetch,
                                       pace_sec=pace_sec, raw_archive=raw_pages)
        collected[product] = rows, source, raw_pages
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".coinbase-pair-stage-", dir=root))
    created_final = False
    try:
        for product in PAIR:
            rows, source, raw_pages = collected[product]
            stem = coinbase.PRODUCTS[product][0].lower() + "_5m"
            csv_payload = _csv_bytes(rows)
            _atomic_write(stage / f"{stem}.csv", csv_payload)
            source["derived_csv_filename"] = f"{stem}.csv"
            source["derived_csv_sha256"] = hashlib.sha256(csv_payload).hexdigest()
            source["raw_payload_bytes_released"] = True
            source["pages"] = [
                {**source["pages"][i], "raw_file": f"raw/{stem}/{i:04d}.json"}
                for i in range(len(raw_pages))
            ]
            for i, (a, b, raw) in enumerate(raw_pages):
                if (source["pages"][i]["from_sec"], source["pages"][i]["to_sec"]) != (a, b):
                    raise AssertionError("Source page collection mismatch")
                _atomic_write(stage / f"raw/{stem}/{i:04d}.json", raw)
            _atomic_write(stage / f"{stem}.source.json", (json.dumps(source, sort_keys=True, indent=2) + "\n").encode())
        verify_bundle(stage)
        audit_bundle_independently(stage)  # Second implementation, before publication.
        if final.exists():
            raise coinbase.SourceError("Run directory appeared concurrently; refusing overwrite")
        os.rename(stage, final)
        created_final = True
        if previous is not None:
            register_verified_bundle(final, manifest_path=manifest_path,
                                     expected_manifest_sha=manifest_sha_at_start)

    finally:
        if not created_final and stage.exists():
            shutil.rmtree(stage)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="UTC 5-min boundary (inclusive)")
    parser.add_argument("--end", help="UTC 5-min boundary (exclusive)")
    parser.add_argument("--verify", type=Path, help="Offline verify an existing two-market archive without fetching")
    parser.add_argument("--register", action="store_true", help="Enable both symbols in local manifest")
    args = parser.parse_args()
    if args.verify is not None:
        if args.start or args.end or args.register:
            parser.error("--verify cannot be mixed with acquisition flags")
        try:
            print("Offline source-to-chart verification:", json.dumps(verify_bundle(args.verify), sort_keys=True))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        return
    if not args.start or not args.end:
        parser.error("--start and --end are required when fetching")
    try:
        folder = export_pair(coinbase.utc_ms(args.start), coinbase.utc_ms(args.end), register=args.register,
                             pace_sec=0.25)
    except bridge.InputError as exc:
        parser.error(str(exc))
    print("Archived Coinbase BTC-USD and ETH-USD 5m source pages:", folder)
    print("Offline reconstruction:", verify_bundle(folder))
    print("No trades. No API credentials. No historical availability claims. No alpha claim.")


if __name__ == "__main__":
    main()
