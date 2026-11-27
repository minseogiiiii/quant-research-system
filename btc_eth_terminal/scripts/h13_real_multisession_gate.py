"""Short, distinct real-session acquisition and registered development study.

CI storage is ephemeral. Only aggregate diagnostics are published.
"""

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from h5_experiment_registry import sha
from h6_registered_strategy_runner import run
from h7_session_walk_forward import run_folds
from h8_qualification import assess
from h13_native_store import capture, import_archive, inventory
from independent_soak_replay import strict_json
from q13_private_fixture_gate import PREFIX, verify_zip

PIN = "8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE = PREFIX + "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
TRIALS = [
    {"product": "BTC-USD", "hold_ms": 5000},
    {"product": "BTC-USD", "hold_ms": 10000},
]


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def gate(archive, output):
    with tempfile.TemporaryDirectory(prefix="h13-real-") as d:
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
        store = Path(d) / "store"
        admitted = [import_archive(store, root, PIN)]
        for _ in range(2):
            # Natural time separation; separate sessions, not a gap-free stream.
            time.sleep(7)
            admitted.append(capture(store, 120, 150_000_000))
            print("H13_SESSION_ADMITTED", len(admitted), flush=True)
        inv = inventory(store)
        cat = inv["catalog"]
        require(
            cat["session_count"] == 3
            and len({r["source_manifest_sha256"] for r in admitted}) == 3
            and inv["unpublished_attempts"] == 0,
            "H13_SESSION_INVENTORY",
        )
        require(
            all(
                r["last_receiver_ms"] - r["first_receiver_ms"] >= 110000
                for r in admitted
            ),
            "H13_SPAN",
        )
        entries = inv["sessions"]
        journal = Path(d) / "trials.db"
        result = run(entries, 5000, TRIALS, 42, journal)
        qualification = assess(
            result,
            journal,
            result["plan_sha256"],
            result["registry_head_sha256"],
            sha(result),
        )
        folds = run_folds(entries, TRIALS, 2, 5000, 42, Path(d) / "folds")
        report = {
            "schema": "h13_real_multisession_pilot_v1",
            "status": "ENGINEERING_DIAGNOSTIC_PASS",
            "sessions": admitted,
            "catalog": cat,
            "registered_research": result,
            "walk_forward": folds,
            "qualification": qualification,
            "new_captures": 2,
            "raw_retained_after_ci": False,
            "persistent_host_verified": False,
            "raw_data_uploaded": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
        with output.open("x") as f:
            json.dump(report, f, sort_keys=True, allow_nan=False)
            f.write("\n")
        print("H13_REAL_MULTISESSION_PIPELINE_PASS", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--release-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    gate(a.release_zip, a.output)
