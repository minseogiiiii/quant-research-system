#!/usr/bin/env python3
"""Restore ONLY the exact SHA-pinned, public-facing shipped v36 bundle.

The private, original full Q7 release ZIP is verified first, not repacked or
fabricated. No raw Coinbase archives, private data or fixtures are extracted.
Runs on ephemeral CI storage. The original Q7 source manifest is unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from q13_private_fixture_gate import PREFIX, verify_zip

RELEASE_PATHS = {
    "index.html",
    "assets/index-BpiBDnMe.js",
    "assets/index-Ckn1nBC7.css",
}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def restore(archive: Path, checkout: Path) -> dict:
    root = (checkout / "btc_eth_terminal/frontend").resolve()
    manifest_file = root / "v36_dist_integrity.json"
    if not manifest_file.is_file():
        raise ValueError("Q15_SHIPPED_MANIFEST_MISSING")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not (
        manifest.get("release") == "v36_engineering_candidate_read_only"
        and manifest.get("trading_enabled") is False
        and set(manifest.get("files", {})) == RELEASE_PATHS
    ):
        raise ValueError("Q15_SHIPPED_MANIFEST_INVALID")
    zip_root = PREFIX + "frontend/dist_v36/"
    with tempfile.TemporaryDirectory(prefix="q15_public_assets_") as workdir:
        temporary = Path(workdir)
        with verify_zip(archive) as zipped:
            selected = {
                name: zipped.getinfo(zip_root + name) for name in RELEASE_PATHS
            }
            for name, info in selected.items():
                entry = manifest["files"][name]
                if info.file_size != entry["bytes"]:
                    raise ValueError("Q15_ARCHIVED_BUNDLE_LENGTH_MISMATCH")
                body = zipped.read(info)
                if len(body) != entry["bytes"] or digest(body) != entry["sha256"]:
                    raise ValueError("Q15_ARCHIVED_BUNDLE_SHA256_MISMATCH")
                candidate = temporary / name
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes(body)
        target = root / "dist_v36"
        if target.is_symlink():
            raise ValueError("Q15_TARGET_SYMLINK_DISALLOWED")
        if target.exists() and (
            {p.relative_to(target).as_posix() for p in target.rglob("*")
             if p.is_file()} - RELEASE_PATHS
        ):
            raise ValueError("Q15_EXISTING_BUNDLE_HAS_UNEXPECTED_FILES")
        target.mkdir(parents=True, exist_ok=True)
        for name in sorted(RELEASE_PATHS):
            destination = target / name
            if destination.is_symlink():
                raise ValueError("Q15_EXISTING_FILE_SYMLINK_DISALLOWED")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temporary / name, destination)
        for name in RELEASE_PATHS:
            dest = target / name
            if digest(dest.read_bytes()) != manifest["files"][name]["sha256"]:
                raise ValueError("Q15_EXTRACTED_ASSET_MISMATCH")
    return {
        "schema": "q15_sha_pinned_public_bundle_from_private_release_v1",
        "restored_shipped_assets": 3,
        "source_zip_verified": True,
        "source_contains_private_data": True,
        "private_data_extracted": False,
        "market_archive_uploaded": False,
        "restored_asset_sha256": {
            name: manifest["files"][name]["sha256"] for name in sorted(RELEASE_PATHS)
        },
        "release_approved": False,
        "trading_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-zip", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    args = parser.parse_args()
    result = restore(args.release_zip, args.checkout_root)
    print("PASS_Q15_GENUINE_SHIPPED_PUBLIC_ASSETS_3_OF_3")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
