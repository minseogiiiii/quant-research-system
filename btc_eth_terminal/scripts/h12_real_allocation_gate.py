"""Real previously inspected source, allocation and joint risk engineering gate."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from q13_private_fixture_gate import verify_zip, PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h1_f6_dataset_replay import replay_dataset
from f5_independent_clock import read_independent
from h9_portfolio import simulate, collect
from h11_risk import POLICY as RISK
from h11_covariance import POLICY as SIZING
from h10_feature_cache import build, load
from h12_portfolio_allocation import allocate

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h12-real-") as d:
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
        require(sig == native, "H12_SIGNAL_PARITY")
        results = {}
        for label, budget in [
            ("standalone_sizing", None),
            ("joint_budget", "0.00002"),
            ("tight_joint_budget", "0.000002"),
        ]:
            a = simulate(
                factory(),
                sig,
                risk_policy=RISK,
                sizing_policy=SIZING,
                joint_budget_fraction=budget,
            )
            b = replay_dataset(
                dest,
                PIN,
                "BTC-USD",
                projector=lambda packets, product, budget=budget: simulate(
                    packets,
                    sig,
                    risk_policy=RISK,
                    sizing_policy=SIZING,
                    joint_budget_fraction=budget,
                ),
                receipt_pin=dp,
            )
            require(
                a == b
                and len(a["portfolios"]) == 10
                and all(x["cash_inventory_witness"] for x in a["portfolios"]),
                "H12_PATH_PARITY",
            )
            results[label] = a
        c = results["standalone_sizing"]["covariance"]
        require(c["ready_estimates"] > 0, "H12_COVARIANCE")
        allocation = allocate(c["last_ready_estimate"])
        require(
            any(
                p["joint_risk"]["rejections"].get("joint_volatility_cap", 0) > 0
                for p in results["tight_joint_budget"]["portfolios"]
            ),
            "H12_JOINT_CAP_PATH",
        )
        result = {
            "schema": "h12_real_allocation_receipt_v1",
            "status": "PASS",
            "source_manifest_sha256": PIN,
            "source_messages": m["messages"],
            "source_kind": "PREVIOUSLY_INSPECTED_HISTORICAL",
            "allocation_asof_ms": c["last_ready_decision_ms"],
            "allocation_covariance": c["last_ready_estimate"],
            "allocation": allocation,
            "results": results,
            "independent_input_path_parity": True,
            "scenario_selection_performed": False,
            "raw_data_uploaded": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
        with output.open("x") as f:
            json.dump(result, f, sort_keys=True, allow_nan=False)
            f.write("\n")
        print("H12_REAL_ALLOCATION_AND_JOINT_RISK_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
