"""One frozen real source, independently verify H1 conversion and F6 path parity."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from f6_liquidity_recovery import POLICY, build, digest
from h1_f6_dataset_replay import replay_dataset
from h1_streaming_parquet import convert
from independent_soak_replay import strict_json
from q13_private_fixture_gate import PREFIX, verify_zip

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def run(archive, output):
    with tempfile.TemporaryDirectory(prefix="h1-f6-") as d:
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
        products = {}
        for p in ("BTC-USD", "ETH-USD"):
            direct = build(root, PIN, p)
            derived = replay_dataset(dest, PIN, p)
            for k in (
                "source_manifest_sha256",
                "epoch",
                "independent_input_parser_parity",
            ):
                direct.pop(k)
            require(direct == derived, "H1_F6_STRATEGY_PARITY")
            products[p] = {
                "strategy_result_sha256": digest(direct),
                "source_messages": direct["source_messages"],
                "signals": direct["signals"],
                "closed_roundtrips": direct["closed_roundtrips"],
                "identical_native_parquet_strategy_result": True,
            }
        report = {
            "schema": "h1_f6_real_path_parity_v1",
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
            json.dump(report, f, sort_keys=True)
            f.write("\n")
        print("H1_F6_REAL_STRATEGY_PARITY_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    run(a.release_zip, a.output)
