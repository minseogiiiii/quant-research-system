"""Fail-closed, read-only historical research bridge for Funnest Charts.

Not a backtest, alpha oracle, market-data provider or execution service. Localhost only.
Research/backtest source files are never modified. Python >=3.11, standard library.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
DATA_ROOT = (HERE / "data").resolve()
MANIFEST = HERE / "manifest.json"
VALID_SOURCES = {"real", "synthetic"}
VALID_EVIDENCE = {"unverified", "source_audited", "oos_tested"}
VALID_TYPES = {"stock", "etf", "index", "crypto", "futures", "forex"}
VALID_SIDES = {"LONG", "SHORT", "WAIT", "FLAT"}
BAR_FIELDS = ("time", "open", "high", "low", "close")
MAX_BYTES = 25 * 1024 * 1024
MAX_REQUEST_BARS = 20_000
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
EPOCH_MS = re.compile(r"\d{12,13}\Z")
FORWARD_FIELD = re.compile(
    (
        '(?:^|[_ .-])(future|label|la'
        'bels|target|targets|forward|'
        'lookahead|lead)(?:$|[_ .-])'
    ),
    re.IGNORECASE,
)


class InputError(ValueError):
    """Invalid/misleading research input; never silently coerce."""


def _time_ms(value: str) -> int:
    raw = str(value).strip()
    if EPOCH_MS.fullmatch(raw):
        result = int(raw)
    elif re.fullmatch(r"\d{10}", raw):
        raise InputError(
            "10-digit epoch seconds forbidden: use 12- or 13-digit epoch milliseconds"
        )
    else:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, OverflowError) as exc:
            raise InputError(f"Invalid timestamp: {raw!r}") from exc
        if dt.utcoffset() is None:
            raise InputError(
                "Timestamp needs an explicit UTC offset, e.g. 2026-11-02T14:30:00Z"
            )
        result = int(dt.timestamp() * 1000)
    if not 631_152_000_000 <= result <= 7_258_118_400_000:
        raise InputError("Timestamp outside supported 1990-2200 range")
    return result


def _num(value: str | None, field: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"Invalid number for {field}: {value!r}") from exc
    if not math.isfinite(x):
        raise InputError(f"Nonfinite {field}")
    return x


def _strict_int(value: str, field: str, max_value: int | None = None) -> int:
    if not re.fullmatch(r"\d+", value):
        raise InputError(f"{field} must be a nonnegative integer")
    n = int(value)
    if max_value is not None and n > max_value:
        raise InputError(f"{field} exceeds limit {max_value}")
    return n


def safe_path(relative: str, *, extension: str = ".csv") -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise InputError("Invalid data filename")
    # Resolving the actual target also blocks symlinks that escape the data directory.
    p = (DATA_ROOT / relative).resolve()
    if not p.is_relative_to(DATA_ROOT) or p.suffix.lower() != extension:
        raise InputError(f"Only {extension} files under backend/data are permitted")
    if any("future_labels" in x.lower() for x in Path(relative).parts):
        raise InputError(
            "Future-label files are evaluation-only and cannot power the chart"
        )
    if not p.is_file():
        raise InputError(f"Missing dataset: {relative}")
    if p.stat().st_size > MAX_BYTES:
        raise InputError(
            "File exceeds 25 MiB prototype limit; pre-aggregate externally"
        )
    return p


def _read_bytes(relative: str, *, extension: str = ".csv") -> tuple[bytes, str]:
    p = safe_path(relative, extension=extension)
    # One bytes snapshot drives BOTH checksum and parsing. Never hash file A
    # and parse file B.
    with p.open("rb") as fp:
        raw = fp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise InputError("File size changed while reading or exceeds limit")
    return raw, hashlib.sha256(raw).hexdigest()


def read_csv(relative: str) -> tuple[list[dict[str, str]], str]:
    raw, digest = _read_bytes(relative)
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise InputError("CSV must be UTF-8") from exc
    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
        names = reader.fieldnames or []
        if not names or len(names) != len(set(names)) or any(not x for x in names):
            raise InputError("CSV header missing or duplicated")
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise InputError("CSV contains extra/missing fields")
    except csv.Error as exc:
        raise InputError(f"Malformed CSV: {exc}") from exc
    return rows, digest


def parse_bars(
    relative: str, *, completed_bar_ms: int | None = None
) -> tuple[list[dict], str]:
    rows, digest = read_csv(relative)
    if not rows:
        raise InputError("No bars; refusing to display an empty dataset")
    if not set(BAR_FIELDS).issubset(rows[0]):
        raise InputError(f"CSV requires {BAR_FIELDS}")
    if any(FORWARD_FIELD.search(name) for name in rows[0]):
        raise InputError("Chart CSV must not contain forward/target/label columns")
    out: list[dict] = []
    prev = -1
    available_present = "available_at" in rows[0]
    for i, r in enumerate(rows, 2):
        t = _time_ms(r["time"])
        o, h, low, c = (_num(r[k], k) for k in ("open", "high", "low", "close"))
        vol = _num(r["volume"], "volume") if r.get("volume") not in ("", None) else None
        if t <= prev:
            raise InputError(
                
                    f"Row {i}: not strictly increasing"
                    f" (duplicate/out-of-order candle timestamp)"
                
            )
        if min(o, h, low, c) <= 0 or low > min(o, c) or h < max(o, c) or low > h:
            raise InputError(f"Row {i}: invalid OHLC or nonpositive financial price")
        if vol is not None and vol < 0:
            raise InputError(f"Row {i}: negative volume")
        record = {"time": t, "open": o, "high": h, "low": low, "close": c}
        if vol is not None:
            record["volume"] = vol
        if available_present:
            avail = _time_ms(r["available_at"])
            if avail < t:
                raise InputError(f"Row {i}: bar available_at predates bar timestamp")
            # A completed OHLCV bar cannot be observed before its close.
            # Only enforce this when the caller explicitly establishes that
            # `time` is bar-open and provides the full interval duration.
            if completed_bar_ms is not None and avail < t + completed_bar_ms:
                raise InputError(
                    f"Row {i}: completed-bar available_at precedes the bar close"
                )
            record["available_at"] = avail
        out.append(record)
        prev = t
    return out, digest


def _parse_dated_events(relative: str, *, kind: str) -> tuple[list[dict], str]:
    rows, digest = read_csv(relative)
    out: list[dict] = []
    ids: set[str] = set()
    prev = -1
    if kind == "signal":
        required = {
            "signal_id",
            "time",
            "available_at",
            "feature_max_observed_at",
            "side",
            "model_id",
        }
    elif kind == "fill":
        required = {"time", "price", "side", "quantity", "event_type"}
    else:
        raise AssertionError(kind)
    if rows and not required.issubset(rows[0]):
        raise InputError(
            f"{kind} CSV missing fields: {sorted(required - set(rows[0]))}"
        )
    for i, r in enumerate(rows, 2):
        t = _time_ms(r["time"])
        if t < prev:
            raise InputError(f"Row {i}: event times not sorted")
        if kind == "signal":
            if r["side"] not in VALID_SIDES:
                raise InputError(f"Row {i}: invalid signal side")
            signal_id = r["signal_id"].strip()
            model_id = r["model_id"].strip()
            if not signal_id or signal_id in ids or not model_id:
                raise InputError(f"Row {i}: duplicate/missing signal_id or model_id")
            ids.add(signal_id)
            avail = _time_ms(r["available_at"])
            feature_max = _time_ms(r["feature_max_observed_at"])
            if avail > t or feature_max > avail:
                raise InputError(
                    
                        f"Row {i}: point-in-time signal leak"
                        f" (feature/availability after decision)"
                    
                )
            out.append(
                {
                    "signal_id": signal_id,
                    "time": t,
                    "available_at": avail,
                    "feature_max_observed_at": feature_max,
                    "side": r["side"],
                    "model_id": model_id,
                    "record_type": "CANDIDATE_SIGNAL",
                }
            )
        else:
            if r["event_type"] not in {"paper_fill", "real_fill"}:
                raise InputError(
                    
                        f"Row {i}: fills file accepts only recorded"
                        f" paper_fill/real_fill, not signals"
                    
                )
            if r["side"] not in {"buy", "sell"}:
                raise InputError(f"Row {i}: fill side must be buy/sell")
            price, qty = _num(r["price"], "price"), _num(r["quantity"], "quantity")
            if price <= 0 or qty <= 0:
                raise InputError(f"Row {i}: fill price and quantity must be positive")
            obj = {
                "time": t,
                "price": price,
                "side": r["side"],
                "quantity": qty,
                "event_type": r["event_type"],
            }
            if "available_at" in r:
                avail = _time_ms(r["available_at"])
                if avail < t:
                    raise InputError(f"Row {i}: fill available_at predates execution")
                obj["available_at"] = avail
            out.append(obj)
        prev = t
    return out, digest


def parse_fills(relative: str) -> list[dict]:
    return _parse_dated_events(relative, kind="fill")[0]


def parse_signals(relative: str) -> list[dict]:
    return _parse_dated_events(relative, kind="signal")[0]


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"Manifest contains ambiguous duplicate key: {key}")
        result[key] = value
    return result


def _reject_manifest_constants(value: str) -> None:
    raise InputError(f"Manifest contains nonfinite JSON constant: {value}")


def read_manifest(path: Path | None = None) -> dict:
    path = path if path is not None else MANIFEST
    try:
        with path.open(encoding="utf-8") as fp:
            doc = json.load(
                fp,
                object_pairs_hook=_reject_duplicate_manifest_keys,
                parse_constant=_reject_manifest_constants,
            )
    except OSError as exc:
        raise InputError(f"Missing manifest: {path}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("datasets"), list):
        raise InputError("Manifest requires datasets array")
    if not isinstance(doc.get("research", []), list):
        raise InputError("Manifest research must be an array")
    if doc.get("scope") == "BTC_ETH_SPOT_5M_RESEARCH_ONLY":
        if doc.get("experiments"):
            raise InputError("BTC/ETH pilot cannot import unrelated frozen experiments")
        if any(
            not isinstance(item, dict)
            or not str(item.get("symbol", "")).upper().startswith(("BTC", "ETH"))
            or item.get("resolution") != "5"
            or item.get("type") != "crypto"
            for item in doc["datasets"]
        ):
            raise InputError(
                "BTC/ETH pilot admits only BTC/ETH crypto 5-minute datasets"
            )
        if any(
            not isinstance(item, dict) or item.get("market") != "BTC/ETH"
            for item in doc.get("research", [])
        ):
            raise InputError("BTC/ETH pilot refuses unrelated research registries")
    pairs: set[tuple[str, str]] = set()
    seen_symbols: dict[str, tuple] = {}
    for item in doc["datasets"]:
        if not isinstance(item, dict):
            raise InputError("Dataset must be an object")
        for k in (
            "symbol",
            "file",
            "resolution",
            "source_type",
            "evidence_status",
            "name",
            "exchange",
            "timezone",
            "session",
            "type",
        ):
            if not isinstance(item.get(k), str) or not item[k]:
                raise InputError(f"Manifest dataset missing {k}")
        if (
            item["source_type"] not in VALID_SOURCES
            or item["evidence_status"] not in VALID_EVIDENCE
        ):
            raise InputError("Invalid source_type/evidence_status")
        if item["type"] not in VALID_TYPES:
            raise InputError("Invalid symbol type")
        # Do not let a hand-written Coinbase manifest bypass the paired raw-archive
        # verifier. The legacy one-product CSV export has no archived HTTP pages.
        if doc.get("scope") == "BTC_ETH_SPOT_5M_RESEARCH_ONLY" and (
            item["exchange"] == "Coinbase Advanced Trade"
            or item["symbol"] in {"BTC_COINBASE_SPOT", "ETH_COINBASE_SPOT"}
        ) and (
            item["exchange"] != "Coinbase Advanced Trade"
            or item["symbol"] not in {"BTC_COINBASE_SPOT", "ETH_COINBASE_SPOT"}
            or item["resolution"] != "5"
            or item["source_type"] != "real"
            or item["evidence_status"] != "unverified"
            or not item.get("provenance_file")
        ):
            raise InputError(
                
                    'Coinbase spot data requires '
                    'canonical identity and paire'
                    'd raw-page provenance'
                
            )
        pair = (item["symbol"], item["resolution"])
        if pair in pairs:
            raise InputError("Duplicate symbol/resolution pair")
        pairs.add(pair)
        identity = tuple(
            item.get(k)
            for k in (
                "name",
                "exchange",
                "timezone",
                "session",
                "type",
                "source_type",
                "fills_file",
                "signals_file",
            )
        )
        if item["symbol"] in seen_symbols and seen_symbols[item["symbol"]] != identity:
            raise InputError(
                
                    'Same symbol must use identic'
                    'al instrument/source metadat'
                    'a across resolutions'
                
            )
        seen_symbols[item["symbol"]] = identity
        if (
            type(item.get("pricePrecision")) is not int
            or not 0 <= item["pricePrecision"] <= 12
        ):
            raise InputError("pricePrecision must be integer 0..12")
        tick = item.get("tickSize")
        if type(tick) not in (int, float) or not math.isfinite(tick) or tick <= 0:
            raise InputError("tickSize must be finite and positive")
        if item["evidence_status"] != "unverified" and item["source_type"] == "real":
            sha = item.get("original_raw_sha256")
            if not isinstance(sha, str) or not SHA256.fullmatch(sha):
                raise InputError(
                    "Audited real dataset requires a 64-character original_raw_sha256"
                )
        safe_path(item["file"])
        for field in ("fills_file", "signals_file"):
            if item.get(field):
                safe_path(item[field])
        if item.get("provenance_file"):
            safe_path(item["provenance_file"], extension=".json")
    return doc


def _registered_bundle_directory(ds: dict) -> Path | None:
    """Validate the exact product-to-archive binding without reading raw pages."""
    source = ds.get("provenance_file")
    if not source:
        return None
    if ds.get("exchange") != "Coinbase Advanced Trade" or ds.get("resolution") != "5":
        raise InputError("Paired archives require Coinbase spot 5-minute metadata")
    product = {"BTC_COINBASE_SPOT": "btc", "ETH_COINBASE_SPOT": "eth"}.get(
        ds.get("symbol")
    )
    if product is None:
        raise InputError("Coinbase bundle must use an exact BTC/ETH spot symbol")
    archive = safe_path(source, extension=".json").parent
    stem = product + "_coinbase_spot_5m"
    if safe_path(ds["file"]) != (archive / (stem + ".csv")).resolve():
        raise InputError(
            "Dataset file must be the CSV verified inside its own source archive"
        )
    if (
        safe_path(source, extension=".json")
        != (archive / (stem + ".source.json")).resolve()
    ):
        raise InputError("Provenance sidecar must match the exact Coinbase instrument")
    return archive


def verify_registered_source(ds: dict) -> None:
    """Re-audit registered raw pages before public reads, not only at catalog load.

    This intentionally has no last-modified-time cache: a modified source must
    not silently pass because its size or timestamp was restored by an editor.
    """
    archive = _registered_bundle_directory(ds)
    if archive is None:
        return
    from fetch_btc_eth import verify_bundle

    verified = verify_bundle(archive)
    if not verified.get("verified") or set(verified.get("markets", {})) != {
        "BTC-USD",
        "ETH-USD",
    }:
        raise InputError("Incomplete paired source archive")


def catalog(doc: dict) -> dict:
    by_symbol: dict[str, list[str]] = {}
    for ds in doc["datasets"]:
        by_symbol.setdefault(ds["symbol"], []).append(ds["resolution"])
    datasets = []
    validated_archives: set[Path] = set()
    for ds in doc["datasets"]:
        # A registered paired Coinbase snapshot is admitted only if BOTH raw
        # source response archives can reproduce BOTH derived CSV files.
        archive = _registered_bundle_directory(ds)
        if archive is not None and archive not in validated_archives:
            verify_registered_source(ds)
            validated_archives.add(archive)
        bars, digest = parse_bars(
            ds["file"],
            completed_bar_ms=300_000 if ds["resolution"] == "5" else None,
        )
        fill_sha = (
            _parse_dated_events(ds["fills_file"], kind="fill")[1]
            if ds.get("fills_file")
            else None
        )
        signal_sha = (
            _parse_dated_events(ds["signals_file"], kind="signal")[1]
            if ds.get("signals_file")
            else None
        )
        datasets.append(
            {
                **ds,
                "csv_sha256": digest,
                "fills_sha256": fill_sha,
                "signals_sha256": signal_sha,
                "row_count": len(bars),
                "earliest_time": bars[0]["time"],
                "latest_time": bars[-1]["time"],
                "supports_asof": "available_at" in bars[0],
                "availability_source_truth_verified": False,
                "supportedResolutions": by_symbol[ds["symbol"]],
                "evidence_label_is_self_declared": True,
                "read_only": True,
            }
        )
    research = []
    for original in doc.get("research", []):
        if not isinstance(original, dict):
            raise InputError("Research record must be an object")
        entry = dict(original)
        if entry.get("ledger_file"):
            raw, digest = _read_bytes(entry["ledger_file"], extension=".json")
            try:
                ledger = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise InputError("Invalid research ledger JSON") from exc
            if not isinstance(ledger, dict):
                raise InputError("Research ledger must contain an object")
            if entry.get("id") == "QQQ-H1":
                entry["status"] = ledger.get("main_hypothesis", {}).get(
                    "status", entry.get("status")
                )
                entry["validated_executable_alpha"] = bool(
                    ledger.get("validated_executable_alpha", 0)
                )
                entry["next_gate"] = ledger.get("next_gate", entry.get("next_gate"))
            entry["ledger_as_of"] = ledger.get("as_of")
            entry["ledger_sha256"] = digest
        if entry.get("report_file"):
            _, digest = _read_bytes(entry["report_file"], extension=".md")
            entry["report_sha256"] = digest
        research.append(entry)
    experiments = []
    for obj in doc.get("experiments", []):
        if (
            not isinstance(obj, dict)
            or not isinstance(obj.get("id"), str)
            or not isinstance(obj.get("file"), str)
        ):
            raise InputError("Malformed experiment summary reference")
        raw, digest = _read_bytes(obj["file"], extension=".json")
        try:
            report = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise InputError("Bad experiment summary JSON") from exc
        if (
            report.get("id") != obj["id"]
            or report.get("status") != "HISTORICAL_PROCESS_EXAMPLE_NOT_ALPHA"
        ):
            raise InputError("Unrecognized experiment summary identity or status")
        experiments.append({**report, "summary_sha256": digest})
    return {
        "datasets": datasets,
        "research": research,
        "experiments": experiments,
        "trading_enabled": False,
        "historical_only": True,
        "derived_data_not_strategy_evidence": True,
    }


def _dataset(doc: dict, symbol: str, resolution: str | None = None) -> dict:
    matches = [
        d
        for d in doc["datasets"]
        if d["symbol"] == symbol
        and (resolution is None or d["resolution"] == resolution)
    ]
    if not matches:
        raise InputError("Unknown symbol/resolution")
    return matches[0]


def get_bars(
    doc: dict,
    symbol: str,
    resolution: str,
    to: int,
    from_: int,
    count_back: int,
    *,
    asof: int | None = None,
) -> dict:
    if to < 0 or from_ < 0 or count_back < 0 or count_back > MAX_REQUEST_BARS:
        raise InputError("Invalid history range/countBack")
    ds = _dataset(doc, symbol, resolution)
    verify_registered_source(ds)
    bars, digest = parse_bars(
        ds["file"],
        completed_bar_ms=300_000 if ds["resolution"] == "5" else None,
    )
    if asof is not None and "available_at" not in bars[0]:
        raise InputError("As-of replay requires explicit available_at for every candle")
    # Exclude bars that were unavailable at the decision/cutoff time.
    allowed = [
        b
        for b in bars
        if b["time"] <= to
        and (asof is None or (b["time"] <= asof and b["available_at"] <= asof))
    ]
    selected = (
        allowed[-count_back:]
        if count_back
        else [b for b in allowed if b["time"] >= from_]
    )
    no_more = not allowed or bool(
        selected and selected[0]["time"] == allowed[0]["time"]
    )
    # No private time/availability audit columns are passed as candle prices.
    public = [{k: v for k, v in b.items() if k != "available_at"} for b in selected]
    return {
        "bars": public,
        "noMoreData": no_more,
        "csv_sha256": digest,
        "replay_asof": asof,
        "n_history_available": len(allowed),
    }


def _get_events(
    doc: dict, symbol: str, kind: str, asof: int | None = None
) -> tuple[list[dict], str | None]:
    ds = _dataset(doc, symbol)
    verify_registered_source(ds)
    key = "fills_file" if kind == "fill" else "signals_file"
    if not ds.get(key):
        return [], None
    events, digest = _parse_dated_events(ds[key], kind=kind)
    if (
        kind == "fill"
        and asof is not None
        and any("available_at" not in e for e in events)
    ):
        raise InputError("As-of replay requires available_at on every fill")
    filtered = [
        e
        for e in events
        if asof is None or (e["time"] <= asof and e["available_at"] <= asof)
    ]
    return filtered, digest


def get_fills(doc: dict, symbol: str, *, asof: int | None = None) -> list[dict]:
    return _get_events(doc, symbol, "fill", asof)[0]


def get_signals(doc: dict, symbol: str, *, asof: int | None = None) -> list[dict]:
    return _get_events(doc, symbol, "signal", asof)[0]


class Handler(BaseHTTPRequestHandler):
    def send_obj(self, code: int, obj: object) -> None:
        raw = json.dumps(obj, allow_nan=False, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "http://localhost:5173")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path in (
                "/terminal",
                "/terminal/index.html",
                "/terminal/app.js",
                "/terminal/styles.css",
            ):
                # Deliberately fixed paths, never a generic directory browser.
                mapping = {
                    "/terminal": "index.html",
                    "/terminal/index.html": "index.html",
                    "/terminal/app.js": "app.js",
                    "/terminal/styles.css": "styles.css",
                }
                root = (HERE.parent / "frontend" / "standalone").resolve()
                target = root / mapping[parsed.path]
                if (
                    parsed.query
                    or target.is_symlink()
                    or not target.is_file()
                    or target.stat().st_size > 250_000
                ):
                    raise InputError("Invalid terminal asset")
                payload = target.read_bytes()
                ext = target.suffix
                content_type = {
                    ".html": "text/html",
                    ".js": "application/javascript",
                    ".css": "text/css",
                }[ext]
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    (
                        "default-src 'none'; script-s"
                        "rc 'self'; style-src 'self';"
                        " connect-src 'self'; img-src"
                        " 'self'; base-uri 'none'; fr"
                        "ame-ancestors 'none'"
                    ),
                )
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path in (
                "/research",
                "/research/index.html",
            ) or parsed.path.startswith("/assets/"):
                # A CI-built React/Funnest bundle, deliberately isolated from the
                # legacy /terminal fallback and every private market-source path.
                # Asset names must be referenced by the exact Vite index manifest.
                if parsed.query:
                    raise InputError("Bundled assets do not accept URL arguments")
                from v33_frontend_integrity import FrontendIntegrityError, get_asset

                try:
                    payload, content_type = get_asset(parsed.path)
                except FrontendIntegrityError:
                    # Fail closed: modified JS/CSS or a spoofed pinned manifest
                    # must block even /research HTML from being served.
                    self.send_obj(
                        503,
                        {
                            "error": (
                                'Pinned frontend bundle is un'
                                'available or failed SHA-256 '
                                'integrity'
                            )
                        },
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'self'; style-src 'self' "
                    "'unsafe-inline'; "
                    "connect-src 'self'; img-src 'self' data:; font-src 'self' data:; "
                    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(payload)
                return
            args = parse_qs(parsed.query)
            # Strict and stable read-only contract: silently ignored or repeated
            # filters can otherwise cause users to misinterpret a research view.
            permitted = {
                "/api/health": set(),
                "/api/catalog": set(),
                "/api/bars": {
                    "symbol",
                    "resolution",
                    "to",
                    "from",
                    "countBack",
                    "asof",
                },
                "/api/signals": {"symbol", "asof"},
                "/api/fills": {"symbol", "asof"},
            }
            permitted.update(
                {
                    "/api/v37/factor-lab": {"product", "asof", "section"},
                    "/api/v30/catalog": set(),
                    "/api/v30/series": {
                        "product",
                        "kind",
                        "from",
                        "to",
                        "limit",
                        "asof",
                        "offset",
                    },
                    "/api/v30/book": {"product", "asof"},
                }
            )
            # Historical v34 routes remain independently pinned to v30 L1,
            # not replayed as a fresh v36 live source.
            permitted.update(
                {
                    "/api/v34/depth": {"product", "asof"},
                    "/api/v34/recovery": {"product", "asof", "limit"},
                }
            )
            if parsed.path not in permitted:
                self.send_obj(404, {"error": "Not found"})
                return
            if any(
                key not in permitted[parsed.path] or len(values) != 1
                for key, values in args.items()
            ):
                raise InputError("Unknown or repeated query parameter")
            if parsed.path == "/api/v37/factor-lab":
                from v37_factor_lab import view as q7_view

                def factor_required(name):
                    values = args.get(name)
                    if not values or len(values) != 1 or not values[0]:
                        raise InputError("Expected exactly one " + name)
                    return values[0]

                product = factor_required("product")
                asof = _strict_int(factor_required("asof"), "asof")
                section = factor_required("section")
                self.send_obj(200, q7_view(product, asof, section))
                return
            if parsed.path.startswith("/api/v34/"):
                from v34_l2_api import depth as v34_depth
                from v34_l2_api import recovery as v34_recovery

                def v34_required(name: str) -> str:
                    value = args.get(name)
                    if not value or len(value) != 1 or not value[0]:
                        raise InputError("Expected exactly one " + name)
                    return value[0]

                product = v34_required("product")
                asof = _strict_int(v34_required("asof"), "asof")
                if parsed.path == "/api/v34/depth":
                    self.send_obj(200, v34_depth(product, asof))
                else:
                    limit = _strict_int(args.get("limit", ["10"])[0], "limit", 30)
                    if limit < 1:
                        raise InputError("Invalid v34 limit")
                    self.send_obj(200, v34_recovery(product, asof, limit))
                return
            if parsed.path.startswith("/api/v30/"):
                from v30_unified_api import book as unified_book
                from v30_unified_api import catalog as unified_catalog
                from v30_unified_api import series as unified_series

                def only(name: str) -> str:
                    v = args.get(name)
                    if not v or len(v) != 1:
                        raise InputError("Expected exactly one " + name)
                    return v[0]

                if parsed.path == "/api/v30/catalog":
                    result = unified_catalog()
                elif parsed.path == "/api/v30/series":
                    result = unified_series(
                        only("product"),
                        only("kind"),
                        _strict_int(only("from"), "from"),
                        _strict_int(only("to"), "to"),
                        _strict_int(args.get("limit", ["100"])[0], "limit", 400),
                        _strict_int(only("asof"), "asof") if "asof" in args else None,
                        _strict_int(args.get("offset", ["0"])[0], "offset", 50_000),
                    )
                else:
                    result = unified_book(
                        only("product"), _strict_int(only("asof"), "asof")
                    )
                self.send_obj(200, result)
                return
            doc = read_manifest()
            if parsed.path == "/api/health":
                self.send_obj(
                    200, {"ok": True, "historical_only": True, "trading_enabled": False}
                )
                return
            if parsed.path == "/api/catalog":
                self.send_obj(200, catalog(doc))
                return

            def required(name: str) -> str:
                values = args.get(name)
                if not values or len(values) != 1:
                    raise InputError(f"Expected exactly one {name}")
                return values[0]

            asof = _time_ms(required("asof")) if "asof" in args else None
            symbol = required("symbol")
            if parsed.path == "/api/bars":
                resolution = required("resolution")
                to = _strict_int(required("to"), "to")
                from_ = _strict_int(args.get("from", ["0"])[0], "from")
                count_back = _strict_int(
                    args.get("countBack", ["0"])[0], "countBack", MAX_REQUEST_BARS
                )
                self.send_obj(
                    200,
                    get_bars(doc, symbol, resolution, to, from_, count_back, asof=asof),
                )
            elif parsed.path == "/api/signals":
                signals, sha = _get_events(doc, symbol, "signal", asof)
                self.send_obj(200, {"signals": signals, "signals_sha256": sha})
            else:
                fills, sha = _get_events(doc, symbol, "fill", asof)
                self.send_obj(200, {"fills": fills, "fills_sha256": sha})
        except (
            InputError,
            ValueError,
            KeyError,
            TypeError,
            FileNotFoundError,
            json.JSONDecodeError,
        ) as exc:
            self.send_obj(400, {"error": str(exc)})
        except Exception as exc:
            self.log_error("bridge internal exception: %s", exc)
            self.send_obj(500, {"error": "Internal data bridge error"})


def main() -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    catalog(
        read_manifest()
    )  # validate configured data, provenance and ledger before serving
    port = int(os.environ.get("QUANT_BRIDGE_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(
        f"LOCAL read-only research bridge: http://127.0.0.1:{port}/api/catalog",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
