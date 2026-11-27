"""Obtain the original v34.1 ZIP only from the existing private archive repo."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from q16_secure_private_archive_download import curl_to_file

BASE = "https://api.github.com/repos/minseogiiiii/q7-private-release-archive"
TAG = "q7-frozen-2026-11-22"
NAME = "btc_eth_unified_terminal_v34_1_semantic_hotfix.zip"
SIZE = 59_570_574
PIN = "8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8"


def fetch(destination: Path):
    token = os.environ.get("Q13_GITHUB_ARCHIVE_TOKEN", "").strip()
    if not token:
        raise ValueError("P0_V34_PRIVATE_TOKEN_MISSING")
    headers = [
        "Accept: application/vnd.github+json",
        "X-GitHub-Api-Version: 2022-11-28",
        f"Authorization: Bearer {token}",
    ]
    if destination.exists() or destination.is_symlink():
        raise ValueError("P0_V34_DESTINATION_EXISTS")
    with tempfile.TemporaryDirectory(prefix="v34_meta_") as folder:
        meta = Path(folder) / "repo.json"
        curl_to_file(BASE, meta, headers, 1_048_576)
        if json.loads(meta.read_bytes()).get("private") is not True:
            raise ValueError("P0_V34_REPOSITORY_NOT_PRIVATE")
        meta = Path(folder) / "release.json"
        curl_to_file(f"{BASE}/releases/tags/{TAG}", meta, headers, 1_048_576)
        release = json.loads(meta.read_bytes())
        matches = [a for a in release["assets"] if a["name"] == NAME]
        if len(matches) != 1:
            raise ValueError("P0_V34_EXACT_PRIVATE_ASSET_NOT_AVAILABLE")
        asset = matches[0]
        if (type(asset.get("id")) is not int or asset["id"] < 1
                or asset.get("size") != SIZE or asset.get("state") != "uploaded"
                or asset.get("digest") not in (None, f"sha256:{PIN}")):
            raise ValueError("P0_V34_ASSET_METADATA_MISMATCH")
        headers[0] = "Accept: application/octet-stream"
        try:
            curl_to_file(f"{BASE}/releases/assets/{asset['id']}", destination,
                         headers, SIZE)
            with destination.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if destination.stat().st_size != SIZE or digest != PIN:
                raise ValueError("P0_V34_ARCHIVE_PIN_MISMATCH")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    print("PASS_EXACT_PRIVATE_V34_1_ORIGINAL")


if __name__ == "__main__":
    try:
        fetch(Path(sys.argv[1]))
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit("P0_V34_PRIVATE_ORIGINAL_UNAVAILABLE_OR_REJECTED") from None
