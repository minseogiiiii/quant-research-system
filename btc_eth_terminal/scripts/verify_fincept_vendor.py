"""Verify exact pinned vendor manifest and complete Git-tracked vendor inventory."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = "btc_eth_terminal/vendor/fincept/"
MANIFEST = VENDOR + "UPSTREAM.json"
MANIFEST_BLOB = "9e65c0e404438bada8ff40a2563fcd43443efdd9"


def blob(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def read_exact(rel: str) -> bytes:
    p = ROOT / rel
    if not p.is_file() or any(
        x.is_symlink() for x in (p, *p.parents) if x.is_relative_to(ROOT)
    ):
        raise SystemExit("VENDOR_FILE_OR_SYMLINK")
    return p.read_bytes()


def main() -> None:
    raw = read_exact(MANIFEST)
    if blob(raw) != MANIFEST_BLOB:
        raise SystemExit("UPSTREAM_MANIFEST_PIN_MISMATCH")
    manifest = json.loads(raw)
    entries = {entry["local"]: entry["git_blob"] for entry in manifest["files"]}
    if len(entries) != 7 or any(not p.startswith(VENDOR) for p in entries):
        raise SystemExit("VENDOR_MANIFEST_INVENTORY")
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "btc_eth_terminal/vendor"], cwd=ROOT
    ).decode().split("\0")
    if set(filter(None, tracked)) != {*entries, MANIFEST}:
        raise SystemExit("VENDOR_TRACKED_INVENTORY_MISMATCH")
    # Reject non-generated additions even if not Git-tracked.
    present = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "btc_eth_terminal/vendor").rglob("*")
        if (p.is_file() or p.is_symlink())
        and "__pycache__" not in p.parts
    }
    if present != {*entries, MANIFEST}:
        raise SystemExit("VENDOR_DISK_INVENTORY_MISMATCH")
    for rel, expected in sorted(entries.items()):
        data = read_exact(rel)
        if blob(data) != expected:
            raise SystemExit("UPSTREAM_PIN_MISMATCH: " + rel)
        print(hashlib.sha256(data).hexdigest(), rel)
    print("PASS_FINCEPT_VENDOR_EXACT_BLOBS")


if __name__ == "__main__":
    main()
