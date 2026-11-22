"""Verify exact v20 release-byte inventory after extraction, no network/orders."""

import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def verify(root: Path):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid/symlink root")
    manifest = root / "RELEASE_FILE_MANIFEST_v20.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Missing release manifest")
    obj = json.loads(manifest.read_bytes())
    if obj.get("release") != "v20" or not isinstance(obj.get("files"), list):
        raise ValueError("Bad release contract")
    known = set()
    for f in obj["files"]:
        name = f.get("path")
        target = root / name if isinstance(name, str) else None
        if (
            not isinstance(name, str)
            or not name
            or name in known
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or "\\" in name
            or target.is_symlink()
            or not target.is_file()
            or not target.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("Invalid release file path or alias")
        known.add(name)
        if target.stat().st_size != f.get("size_bytes") or sha(target) != f.get(
            "sha256"
        ):
            raise ValueError("Release size/hash changed: " + name)
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
        and p.name != "RELEASE_FILE_MANIFEST_v20.json"
    }
    if known != actual:
        raise ValueError(
            "Unexpected/missing release files: "
            + str(sorted(known.symmetric_difference(actual))[:15])
        )
    return {
        "status": "V20_INDEPENDENT_CLEAN_EXTRACTION_SHA256_PASSED",
        "files": len(known),
        "source_data_integrity_verified": True,
        "independent_market_truth_verified": False,
        "actual_24h_source_verified": False,
        "real_orders_sent": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    args = p.parse_args()
    print(json.dumps(verify(args.root), indent=2))


if __name__ == "__main__":
    main()
