"""Pinned native archive and Parquet cost-aware rebalance parity."""

import argparse
import hashlib
import json
import shutil
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path

from f5_independent_clock import read_independent
from h1_f6_dataset_replay import replay_dataset
from h1_streaming_parquet import convert
from h12_portfolio_allocation import allocate
from h12_rebalance import POLICY, project, shrink
from independent_soak_replay import strict_json
from q13_private_fixture_gate import PREFIX, verify_zip

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h12ef-real-") as d:
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
        results = {}
        policies = [
            ("no_deadband", dict(POLICY, deadband_fraction="0")),
            ("cost_aware", POLICY),
            ("shrinkage", dict(POLICY, shrinkage="0.25")),
            ("zero_cost_budget", dict(POLICY, max_cost_bps="0")),
        ]
        for label, p in policies:
            a = project(read_independent(root, m), p)
            b = replay_dataset(
                dest,
                PIN,
                "BTC-USD",
                projector=lambda packets, product, p=p: project(packets, p),
                receipt_pin=dp,
            )
            require(
                a == b
                and a["source_messages"] == m["messages"]
                and a["cash_inventory_witness"],
                "H12EF_PATH",
            )
            require(a["covariance"]["ready_estimates"] > 0, "H12EF_COVARIANCE")
            results[label] = a
        require(
            results["zero_cost_budget"]["counts"]["fills"] == 0
            and results["zero_cost_budget"]["counts"]["decision_cost_rejected"] > 0,
            "H12EF_COST_PATH",
        )
        require(results["no_deadband"]["counts"]["fills"] > 0, "H12EF_REAL_FILL_PATH")
        e = results["cost_aware"]["covariance"]["last_ready_estimate"]
        raw = {
            "variance": {k: Decimal(v) for k, v in e["variance"].items()},
            "covariance": Decimal(e["covariance"]),
        }
        with localcontext() as ctx:
            ctx.prec = 80
            adj = shrink(raw, Decimal(".25"))
            require(
                adj["variance"] == raw["variance"]
                and adj["covariance"] == Decimal(".75") * raw["covariance"],
                "H12EF_SHRINKAGE",
            )
        allocation = {"raw": allocate(raw), "diagonal_25pct": allocate(adj)}
        result = {
            "schema": "h12ef_real_rebalance_receipt_v1",
            "status": "PASS",
            "source_manifest_sha256": PIN,
            "source_messages": m["messages"],
            "source_kind": "PREVIOUSLY_INSPECTED_HISTORICAL",
            "results": results,
            "allocation": allocation,
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
        print("H12EF_REAL_REBALANCE_SHRINKAGE_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
