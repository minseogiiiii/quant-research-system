"""Real previously inspected native archive, same-policy path parity only."""

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
from h3_momentum_horizons import project, POLICY
from h9_portfolio import collect, simulate

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h9-real-") as d:
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
        receipt_pin = hashlib.sha256((dest / "dataset.json").read_bytes()).hexdigest()
        def factory():
            return read_independent(root, m)
        sig, reports = collect(factory)
        other = []
        for product in reports:
            signals = []

            def projector(packets, product, *, signals=signals):
                return project(packets, product, signal_sink=signals.append)

            r = replay_dataset(
                dest, PIN, product, projector=projector, receipt_pin=receipt_pin
            )
            require(r == reports[product], "H9_SIGNAL_REPLAY")
            other.extend(signals)
        require(sorted(other) == sig, "H9_SIGNAL_PARITY")
        a = simulate(factory(), sig)
        b = replay_dataset(
            dest,
            PIN,
            "BTC-USD",
            projector=lambda packets, product: simulate(packets, sorted(other)),
            receipt_pin=receipt_pin,
        )
        require(
            a == b
            and len(a["portfolios"]) == 10
            and a["source_messages"] == m["messages"],
            "H9_PORTFOLIO_PARITY",
        )
        a.update(
            status="ENGINEERING_DIAGNOSTIC_PASS",
            source_kind="PREVIOUSLY_INSPECTED_HISTORICAL",
            source_manifest_sha256=PIN,
            independent_input_path_parity=True,
            raw_data_uploaded=False,
        )
        with output.open("x") as f:
            json.dump(a, f, sort_keys=True, allow_nan=False)
            f.write("\n")
        print("H9_REAL_PORTFOLIO_PARITY_PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
