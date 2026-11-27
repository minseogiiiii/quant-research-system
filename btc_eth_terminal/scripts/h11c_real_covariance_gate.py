"""Pinned historical engineering diagnostic, native/Parquet sizing parity."""

import argparse
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path
from statistics import covariance, stdev, correlation
from decimal import Decimal
from q13_private_fixture_gate import verify_zip, PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h1_f6_dataset_replay import replay_dataset
from f5_independent_clock import read_independent
from h9_portfolio import simulate, collect
from h11_risk import POLICY as RISK
from h11_covariance import POLICY
from h3_momentum_horizons import POLICY as SIGNAL
from h10_feature_cache import build, load
from f6_liquidity_recovery import digest
from post_quote_markout import IndexedBook
from f4_depth_buckets import units

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
PRODUCTS = ("BTC-USD", "ETH-USD")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def witness(packets, window):
    # Separate simple dictionary order book, direct aligned grid arithmetic
    # and Python statistics; no shared RollingCovariance or size routine.
    books = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
    clocks = dict.fromkeys(PRODUCTS, None)
    decision = None
    grid = SIGNAL["grid_ms"]
    age = SIGNAL["max_age_ms"]
    prev = None
    returns = []
    points = missing = ready = 0
    latest = None
    last_ready = None
    for _seq, t, w in packets:
        if decision is None:
            decision = ((t + grid - 1) // grid) * grid
        while decision <= t:
            points += 1
            if any(
                clocks[p] is None
                or not clocks[p] < decision
                or decision - clocks[p] > age
                for p in PRODUCTS
            ):
                prev = None
                returns = []
                missing += 1
            else:
                mid = [
                    (max(books[p]["bid"]) + min(books[p]["offer"])) / 2
                    for p in PRODUCTS
                ]
                if prev is not None:
                    returns.append(
                        tuple(float((v - old) / old) for v, old in zip(mid, prev, strict=True))
                    )
                    returns = returns[-window:]
                prev = mid
                if len(returns) == window:
                    a, b = map(list, zip(*returns, strict=True))
                    sa, sb = stdev(a), stdev(b)
                    latest = (
                        covariance(a, b),
                        sa,
                        sb,
                        correlation(a, b) if sa and sb else None,
                    )
                    ready += 1
                    last_ready = decision
            decision += grid
        if w["channel"] != "l2_data":
            continue
        for e in w["events"]:
            p = e["product_id"]
            if e["type"] == "snapshot":
                books[p] = {"bid": {}, "offer": {}}
            for u in e["updates"]:
                level = books[p][u["side"]]
                price = Decimal(u["price_level"])
                q = Decimal(u["new_quantity"])
                if q:
                    level[price] = q
                else:
                    level.pop(price, None)
            clocks[p] = t
    return points, missing, ready, last_ready, latest


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h11c-real-") as d:
        root = Path(d) / "raw"
        root.mkdir()
        with verify_zip(archive) as z:
            manifest = z.read(SOURCE + "manifest.json")
            require(hashlib.sha256(manifest).hexdigest() == PIN, "F4_REAL_PIN")
            m = strict_json(manifest)
            require(m.get("synthetic_fixture") is not True, "F4_REAL_SYNTHETIC")
            (root / "manifest.json").write_bytes(manifest)
            (root / "chunks").mkdir()
            for c in m["chunks"]:
                import re

                name = c["file"]
                require(
                    re.fullmatch(r"chunks/chunk_\d{5}_conn_\d{3}\.jsonl", name),
                    "F4_REAL_PATH",
                )
                info = z.getinfo(SOURCE + name)
                require(
                    info.file_size == c["archived_line_bytes"]
                    and info.file_size <= 32000000,
                    "F4_REAL_SIZE",
                )
                with z.open(info) as src, (root / name).open("xb") as dst:
                    shutil.copyfileobj(src, dst)
        dest = Path(d) / "dataset"
        convert(root, PIN, dest)
        dp = hashlib.sha256((dest / "dataset.json").read_bytes()).hexdigest()
        cache = Path(d) / "cache"
        _, cp = build(dest, PIN, dp, cache)
        sig, _, _ = load(cache, cp, PIN, dp)
        def factory():
            return read_independent(root, m)
        native, _ = collect(factory)
        require(sig == native, "H11C_SIGNAL_PARITY")
        results = {}
        for label, p in [
            ("fixed", None),
            ("volatility_sized", POLICY),
            ("tight_budget", dict(POLICY, risk_budget_fraction="0.000005")),
        ]:
            a = simulate(factory(), sig, risk_policy=RISK, sizing_policy=p)
            b = replay_dataset(
                dest,
                PIN,
                "BTC-USD",
                projector=lambda packets, product, p=p: simulate(
                    packets, sig, risk_policy=RISK, sizing_policy=p
                ),
                receipt_pin=dp,
            )
            require(
                a == b
                and len(a["portfolios"]) == 10
                and all(x["cash_inventory_witness"] for x in a["portfolios"]),
                "H11C_PATH_PARITY",
            )
            if p is not None:
                points, missing, ready, t, latest = witness(
                    factory(), p["window_returns"]
                )
                c = a["covariance"]
                require(
                    (points, missing, ready, t)
                    == (
                        c["grid_points"],
                        c["missing_paired_marks"],
                        c["ready_estimates"],
                        c["last_ready_decision_ms"],
                    )
                    and ready > 0,
                    "H11C_WINDOW_PARITY",
                )
                e = c["last_ready_estimate"]
                actual = (
                    float(e["covariance"]),
                    float(e["volatility"]["BTC-USD"]),
                    float(e["volatility"]["ETH-USD"]),
                    None if e["correlation"] is None else float(e["correlation"]),
                )
                require(
                    all(
                        x is None
                        and y is None
                        or x is not None
                        and y is not None
                        and math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12)
                        for x, y in zip(actual, latest, strict=True)
                    ),
                    "H11C_NUMERIC_PARITY",
                )
            results[label] = a
        result = {
            "schema": "h11c_real_covariance_receipt_v1",
            "status": "PASS",
            "source_manifest_sha256": PIN,
            "source_messages": m["messages"],
            "source_kind": "PREVIOUSLY_INSPECTED_HISTORICAL",
            "results": results,
            "independent_input_path_parity": True,
            "independent_covariance_witness": True,
            "scenario_selection_performed": False,
            "raw_data_uploaded": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
        with output.open("x") as f:
            json.dump(result, f, sort_keys=True, allow_nan=False)
            f.write("\n")
        print("H11C_REAL_COVARIANCE_SIZING_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
