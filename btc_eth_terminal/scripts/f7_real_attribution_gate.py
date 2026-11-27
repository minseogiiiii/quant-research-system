"""Pinned previously inspected 120-second archive; engineering diagnostic only."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from q13_private_fixture_gate import verify_zip, PREFIX
from independent_soak_replay import strict_json
from f4_order_flow import read_packets
from f5_independent_clock import read_independent
from f6_liquidity_recovery import build, POLICY, digest
from f7_pnl_attribution import summarize
from f6_fincept_metrics_adapter import evaluate
from f7_cost_sensitivity import sensitivity

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def run(archive, output):
    with tempfile.TemporaryDirectory(prefix="f7-real-") as d:
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
        products = {}
        for p in ("BTC-USD", "ETH-USD"):
            expected = build(root, PIN, p)
            for k in (
                "source_manifest_sha256",
                "epoch",
                "independent_input_parser_parity",
            ):
                expected.pop(k)
            a, report = summarize(read_packets(root, m), p)
            b, independent = summarize(read_independent(root, m), p)
            require(
                a == b == expected and report == independent, "F7_NATIVE_TRACE_PARITY"
            )
            returns = report.pop("returns")
            timestamps = report.pop("timestamps")
            if report["complete_regular_grid"] and len(returns) >= 2:
                metrics = evaluate(returns, timestamps, report["interval_ms"])
                # A two-minute diagnostic is not a meaningful annualized
                # performance or tail-risk estimate. Suppress those outputs.
                if metrics["short_sample_warning"]:
                    for k in (
                        "annualized_sample_sharpe",
                        "return_var_95",
                        "return_cvar_95",
                    ):
                        metrics[k] = None
                report["fincept_grid_metrics"] = metrics
                report["metrics_scope"] = "OBSERVED_REGULAR_MARK_TO_MID_WINDOW_ONLY"
            else:
                report["fincept_grid_metrics"] = None
                report["metrics_scope"] = "UNAVAILABLE_INCOMPLETE_GRID"
            report["fixed_path_cost_sensitivity"] = sensitivity(report, POLICY)
            products[p] = report
        result = {
            "schema": "f7_real_attribution_receipt_v1",
            "status": "PASS",
            "source_manifest_sha256": PIN,
            "policy_sha256": digest(POLICY),
            "products": products,
            "raw_data_uploaded": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
        with output.open("x") as f:
            json.dump(result, f, sort_keys=True, allow_nan=False)
            f.write("\n")
        print("F7_REAL_ATTRIBUTION_PARITY_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    run(a.release_zip, a.output)
