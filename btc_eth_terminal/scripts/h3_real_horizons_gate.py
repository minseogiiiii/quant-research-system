"""Fixed horizon diagnostic: pinned history and distinct 20-minute public capture."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from q13_private_fixture_gate import verify_zip, PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h1_f6_dataset_replay import replay_dataset
from f5_independent_clock import read_independent
from f6_liquidity_recovery import digest
from h3_momentum_horizons import project, POLICY

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
PRODUCTS = ("BTC-USD", "ETH-USD")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def summarize(root, pin, kind, min_span):
    with tempfile.TemporaryDirectory(prefix="h3-derived-") as d:
        dest = Path(d) / "dataset"
        receipt = convert(root, pin, dest)
        m = strict_json((root / "manifest.json").read_bytes())
        products = {}
        for p in PRODUCTS:
            a = project(read_independent(root, m), p)
            b = replay_dataset(dest, pin, p, projector=project)
            require(
                a == b
                and a["source_messages"] == receipt["source_messages"]
                and a["policy_sha256"] == digest(POLICY),
                "H3_PATH_PARITY",
            )
            require(
                a["source_last_ms"] - a["source_first_ms"] >= min_span, "H3_SOURCE_SPAN"
            )
            products[p] = a
    return {
        "schema": "h3_real_horizon_pilot_v1",
        "status": "ENGINEERING_DIAGNOSTIC_PASS",
        "source_kind": kind,
        "source_manifest_sha256": pin,
        "source_sha": os.environ.get("GITHUB_SHA"),
        "policy_sha256": digest(POLICY),
        "products": products,
        "raw_data_uploaded": False,
        "independent_parser_and_parquet_result_parity": True,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }


def historical(archive):
    with tempfile.TemporaryDirectory(prefix="h3-historical-") as d:
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
        return summarize(root, PIN, "PREVIOUSLY_INSPECTED_HISTORICAL", 110000)


def forward(prior):
    require(
        prior["source_kind"] == "PREVIOUSLY_INSPECTED_HISTORICAL"
        and prior["status"] == "ENGINEERING_DIAGNOSTIC_PASS"
        and prior["source_manifest_sha256"] == PIN
        and prior["source_sha"] == os.environ.get("GITHUB_SHA")
        and prior["policy_sha256"] == digest(POLICY)
        and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
        "H3_PRIOR",
    )
    with tempfile.TemporaryDirectory(prefix="h3-forward-") as d:
        work = Path(d)
        collector = Path(__file__).resolve().with_name("btc_eth_multiday_v20.mjs")
        env = {
            "PATH": os.environ["PATH"],
            "SOAK_DURATION_SECONDS": "1200",
            "SOAK_RAW_BUDGET_BYTES": "600000000",
            "SOAK_ARCHIVE_BUDGET_BYTES": "900000000",
            "SOAK_DISK_RESERVE_BYTES": "2147483648",
            "SOAK_CHUNK_LIMIT_BYTES": "8000000",
        }
        proc = subprocess.run(
            ["node", str(collector)],
            cwd=work,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1290,
        )
        require(proc.returncode == 0, "H3_FORWARD_CAPTURE_NOT_CLEAN")
        roots = list((work / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        require(len(roots) == 1, "H3_INVENTORY")
        root = roots[0]
        raw = (root / "manifest.json").read_bytes()
        m = strict_json(raw)
        pin = hashlib.sha256(raw).hexdigest()
        require(
            pin != PIN
            and m.get("synthetic_fixture") is False
            and m.get("finish_reason") == "DURATION_REACHED"
            and m.get("total_reconnects") == 0
            and m.get("faults") == [],
            "H3_CLEAN",
        )
        result = summarize(root, pin, "NEW_PUBLIC_20_MINUTE_CAPTURE", 1190000)
        require(
            all(
                result["products"][p]["source_first_ms"]
                > prior["products"][p]["source_last_ms"]
                for p in PRODUCTS
            ),
            "H3_NEW_SOURCE",
        )
        result["historical_manifest_sha256"] = PIN
        result["frozen_policy_matches_historical"] = True
        return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path)
    p.add_argument("--historical-receipt", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    require(bool(a.release_zip) != bool(a.historical_receipt), "H3_MODE")
    result = (
        historical(a.release_zip)
        if a.release_zip
        else forward(strict_json(a.historical_receipt.read_bytes()))
    )
    with a.output.open("x") as f:
        json.dump(result, f, sort_keys=True, allow_nan=False)
        f.write("\n")
    print("H3_" + result["source_kind"] + "_PASS", flush=True)


if __name__ == "__main__":
    main()
