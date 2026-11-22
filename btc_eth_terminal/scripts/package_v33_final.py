#!/usr/bin/env python3
"""Build v33 immutable distribution and exact-parent patch with checked deletes."""

from __future__ import annotations

import json
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V32 = Path("/mnt/data/btc_eth_unified_terminal_v32.zip")
FULL = Path("/mnt/data/btc_eth_unified_terminal_v33_final_verified.zip")
PATCH = Path("/mnt/data/btc_eth_unified_terminal_v33_final_verified_patch.zip")
sys.path.insert(0, str(ROOT / "scripts"))
from apply_v33_patch import (
    PARENT_SHA,
    PATCH_META,
    TARGET_MANIFEST,
    collect,
    reconstruct,
    safe_name,
    sha,
)


def run():
    if sha(V32.read_bytes()) != PARENT_SHA:
        raise ValueError("v32 parent ZIP hash mismatch")
    with zipfile.ZipFile(V32) as z:
        parent = collect(z)
    current = {}
    for f in sorted(ROOT.rglob("*")):
        if (
            not f.is_file()
            or f.is_symlink()
            or "__pycache__" in f.parts
            or f.suffix in (".pyc", ".pyo")
        ):
            continue
        name = safe_name(f.relative_to(ROOT).as_posix())
        if name == TARGET_MANIFEST:
            continue
        current[name] = f.read_bytes()
    # ZIPs are immutable archives; no previous actual backend/data bytes may change.
    data = [p for p in parent if p.startswith("backend/data/")]
    changed_data = [p for p in data if current.get(p) != parent[p]]
    if changed_data:
        raise ValueError("Original market evidence changed: " + repr(changed_data[:3]))
    removed = {p: sha(parent[p]) for p in parent if p not in current}
    changed = {
        p: sha(raw) for p, raw in current.items() if p not in parent or parent[p] != raw
    }
    manifest = {
        "release": "v33",
        "parent": "v32",
        "parent_zip_sha256": PARENT_SHA,
        "files": [
            {"path": p, "sha256": sha(raw), "size_bytes": len(raw)}
            for p, raw in sorted(current.items())
        ],
        "original_backend_data_preserved": len(data),
        "funnest_ci_build_pinned": True,
        "npm_ci_lock_committed": True,
        "browser_historical_only": True,
        "actual_professional_ui_compiled": True,
        "l1_only_ladder": True,
        "liquidity_recovery_unmeasured": True,
        "live_streaming_implemented": False,
        "original_feature_label_pit_verified": False,
        "blind_oos_approved": False,
        "actual_24h_capture_verified": False,
        "executable_alpha_verified": False,
        "real_orders": 0,
    }
    current[TARGET_MANIFEST] = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode()
    meta = {
        "schema": "v33_exact_parent_patch_v1",
        "parent_zip_sha256": PARENT_SHA,
        "deleted_paths": removed,
        "changed_paths": {
            p: sha(raw)
            for p, raw in sorted(current.items())
            if p not in parent or parent[p] != raw
        },
    }

    def write_zip(file, entries):
        with zipfile.ZipFile(
            file,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as z:
            for p, raw in sorted(entries.items()):
                i = zipfile.ZipInfo(p, date_time=(2026, 11, 16, 0, 0, 0))
                i.compress_type = zipfile.ZIP_DEFLATED
                i.external_attr = (stat.S_IFREG | 0o644) << 16
                z.writestr(i, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)

    write_zip(FULL, current)
    patch_entries = {p: current[p] for p in meta["changed_paths"]}
    patch_entries[PATCH_META] = (
        json.dumps(meta, sort_keys=True, indent=2) + "\n"
    ).encode()
    write_zip(PATCH, patch_entries)
    verified = reconstruct(V32, PATCH)
    if verified != current:
        raise ValueError("Patch overlay not byte-identical to full release")
    with zipfile.ZipFile(FULL) as z:
        if collect(z) != current:
            raise ValueError("Full clean ZIP not byte-identical")
    evidence = {
        "status": "PASS",
        "parent_sha256": PARENT_SHA,
        "full_sha256": sha(FULL.read_bytes()),
        "patch_sha256": sha(PATCH.read_bytes()),
        "full_files": len(current),
        "manifest_hashed_files": len(manifest["files"]),
        "patch_files": len(patch_entries),
        "patch_deletions": removed,
        "patch_changed_count": len(meta["changed_paths"]),
        "original_data_unchanged_count": len(data),
        "full_size_bytes": FULL.stat().st_size,
        "patch_size_bytes": PATCH.stat().st_size,
        "clean_full_and_exact_parent_patch_equal": True,
        "actual_24h_capture_verified": False,
        "no_executable_alpha_claim": True,
        "real_orders": 0,
    }
    Path("/mnt/data/V33_FINAL_PACKAGE_VERIFICATION.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {k: v for k, v in evidence.items() if k != "patch_deletions"}, indent=2
        )
    )


if __name__ == "__main__":
    run()
