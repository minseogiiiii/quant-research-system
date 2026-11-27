"""Real source risk admission and path parity; no strategy selection."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from decimal import Decimal
from q13_private_fixture_gate import verify_zip, PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h1_f6_dataset_replay import replay_dataset
from f5_independent_clock import read_independent
from h9_portfolio import simulate, collect
from h11_risk import POLICY
from h10_feature_cache import build, load
from f6_liquidity_recovery import digest

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h11-real-") as d:
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
        factory = lambda: read_independent(root, m)
        native, _ = collect(factory)
        require(sig == native, "H11_SIGNAL_PARITY")
        policies = [
            ("baseline", None),
            ("default", POLICY),
            (
                "tight_exposure",
                dict(POLICY, per_product_fraction="0.03", gross_fraction="0.04"),
            ),
            (
                "tight_loss",
                dict(
                    POLICY, max_loss_fraction="0.00001", max_drawdown_fraction="0.00001"
                ),
            ),
        ]
        results = {}
        for label, policy in policies:
            a = simulate(factory(), sig, risk_policy=policy)
            b = replay_dataset(
                dest,
                PIN,
                "BTC-USD",
                projector=lambda packets, product: simulate(
                    packets, sig, risk_policy=policy
                ),
                receipt_pin=dp,
            )
            require(a == b and len(a["portfolios"]) == 10, "H11_PATH_PARITY")
            require(
                all(p["cash_inventory_witness"] for p in a["portfolios"]), "H11_LEDGER"
            )
            if policy is not None:
                for p in a["portfolios"]:
                    r = p["risk"]
                    require(
                        sum(r["entry_rejections"].values())
                        <= p["counts"]["failed_entries"]
                        and r["risk_limits_guaranteed"] is False
                        and r["forced_liquidation"] is False,
                        "H11_RISK_COUNTS",
                    )
            results[label] = a
        require(
            any(
                p["risk"]["entry_rejections"].get("risk_product_cap", 0) > 0
                for p in results["tight_exposure"]["portfolios"]
            ),
            "H11_EXPOSURE_PATH",
        )
        require(
            any(
                p["risk"]["entry_latch"] is not None
                for p in results["tight_loss"]["portfolios"]
            ),
            "H11_LATCH_PATH",
        )
        result = {
            "schema": "h11_real_risk_receipt_v1",
            "status": "PASS",
            "source_manifest_sha256": PIN,
            "source_messages": m["messages"],
            "source_kind": "PREVIOUSLY_INSPECTED_HISTORICAL",
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
        print("H11_REAL_RISK_PATH_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
