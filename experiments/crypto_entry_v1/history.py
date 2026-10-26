"""Binance Vision SPOT 1m archive importer; no API key needed.

DOES NOT assert that a Binance Global signal is executable in the U.S.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
import pandas as pd

COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "n_trades", "taker_buy_base",
        "taker_buy_quote", "ignore"]
NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume",
           "n_trades", "taker_buy_base", "taker_buy_quote"]
BASE = "https://data.binance.vision/data/spot/monthly/klines"


def fetch(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "crypto-entry-research/0.1"}), timeout=60) as r:
        return r.read()


def load_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        csvs = [x for x in archive.namelist() if x.endswith(".csv")]
        if len(csvs) != 1:
            raise ValueError(f"Expected exactly 1 CSV in {path}")
        with archive.open(csvs[0]) as stream:
            df = pd.read_csv(stream, header=None, names=COLS, low_memory=False)
    # Binance historical archives can have header rows; do NOT silently
    # convert malformed rows to NaN and discard them.
    if str(df.iloc[0]["open_time"]).lower().strip() in {"open time", "open_time"}:
        df = df.iloc[1:].copy()
    raw_time = pd.to_numeric(df["open_time"], errors="raise").astype("int64")
    # 2025+ Binance spot files use microseconds; earlier files milliseconds.
    time_unit = "us" if raw_time.median() > 1e14 else "ms"
    df["ts"] = pd.to_datetime(raw_time, unit=time_unit, utc=True)
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="raise")
    df = df[["ts", *NUMERIC]].reset_index(drop=True)
    if df["ts"].duplicated().any() or not df["ts"].is_monotonic_increasing:
        raise ValueError(f"Duplicate/unsorted timestamps in {path}")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"Nonpositive OHLC in {path}")
    if ((df["high"] < df[["open", "close", "low"]].max(axis=1)) |
        (df["low"] > df[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError(f"Invalid OHLC in {path}")
    if (df["taker_buy_quote"] < 0).any() or (df["taker_buy_quote"] > df["quote_volume"] * (1 + 1e-7)).any():
        raise ValueError(f"Invalid taker buy quote volume in {path}")
    return df


def verify_zip(raw: bytes, checksum: str, filename: str) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    first = checksum.strip().split()[0].lower()
    if first != digest:
        raise ValueError(f"SHA256 mismatch: {filename}: expected={first}, actual={digest}")
    return digest


def download_months(symbol: str, months: list[str], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    for month in months:
        file = f"{symbol}-1m-{month}.zip"
        url = f"{BASE}/{symbol}/1m/{file}"
        raw = fetch(url)
        chk = fetch(f"{url}.CHECKSUM").decode("utf-8")
        digest = verify_zip(raw, chk, file)
        fp = out / file
        fp.write_bytes(raw)
        data = load_zip(fp)
        manifest.append({"name": file, "source": url, "sha256": digest,
                         "rows": len(data), "start": str(data.ts.iloc[0]),
                         "end": str(data.ts.iloc[-1])})
        print(f"VERIFIED {file} rows={len(data)} sha256={digest[:16]}", flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))


def read_months(directory: Path) -> pd.DataFrame:
    archives = sorted(directory.glob("*-1m-????-??.zip"))
    if not archives:
        raise FileNotFoundError(f"No monthly archives in {directory}")
    manifest_path = directory / "manifest.json"
    manifest = {x["name"]: x for x in json.loads(manifest_path.read_text())} if manifest_path.exists() else {}
    frames = []
    for fp in archives:
        if fp.name in manifest:
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            if actual != manifest[fp.name]["sha256"]:
                raise ValueError(f"Changed raw archive {fp.name}")
        frames.append(load_zip(fp))
    df = pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)
    if df.ts.duplicated().any():
        raise ValueError("Duplicate minutes across monthly files")
    delta = df.ts.diff().dropna()
    if (delta != pd.Timedelta(minutes=1)).any():
        gaps = int((delta != pd.Timedelta(minutes=1)).sum())
        raise ValueError(f"Dataset has {gaps} discontinuities; fail closed; do not compute rolling features across gaps")
    return df


def month_range(start: str, end: str) -> list[str]:
    a = pd.Period(start, freq="M")
    b = pd.Period(end, freq="M")
    if b < a:
        raise ValueError("End precedes start")
    return [str(x) for x in pd.period_range(a, b, freq="M")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--from-month", default="2024-01")
    ap.add_argument("--to-month", default="2026-09")
    ap.add_argument("--out", type=Path, default=Path("data/binance/BTCUSDT"))
    args = ap.parse_args()
    download_months(args.symbol, month_range(args.from_month, args.to_month), args.out)


if __name__ == "__main__":
    main()