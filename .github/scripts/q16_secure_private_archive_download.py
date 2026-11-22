#!/usr/bin/env python3
"""Download frozen Q7 from private GitHub Releases or HTTPS signed storage.

No secret or URL is logged. GitHub tokens are confined to exact GitHub API
Release paths. The complete original ZIP must pass fixed size and SHA-256.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from q13_private_fixture_gate import ARCHIVE_SHA256, sha256

EXPECTED_BYTES = 61_656_622
ASSET_NAME = "BTC_ETH_V36_3_R5_Q7_VERIFIED_ENGINEERING_CANDIDATE.zip"
GITHUB_OWNER_REPO = r"/repos/minseogiiiii/[A-Za-z0-9_.-]+"
GITHUB_ASSET_PATH = re.compile(
    GITHUB_OWNER_REPO + r"/releases/assets/[1-9][0-9]*"
)
GITHUB_TAG_PATH = re.compile(
    GITHUB_OWNER_REPO + r"/releases/tags/[A-Za-z0-9_.-]+"
)


def request_kind(url: str, token: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Q16_PRIVATE_ARCHIVE_URL_MUST_BE_HTTPS")
    if parsed.fragment:
        raise ValueError("Q16_PRIVATE_ARCHIVE_FRAGMENT_DISALLOWED")
    if parsed.hostname == "api.github.com":
        if parsed.netloc != "api.github.com" or parsed.query:
            raise ValueError("Q16_GITHUB_RELEASE_ENDPOINT_REQUIRED")
        if GITHUB_ASSET_PATH.fullmatch(parsed.path):
            kind = "github_asset"
        elif GITHUB_TAG_PATH.fullmatch(parsed.path):
            kind = "github_tag"
        else:
            raise ValueError("Q16_GITHUB_RELEASE_ENDPOINT_REQUIRED")
        if not token:
            raise ValueError("Q16_GITHUB_READONLY_ARCHIVE_TOKEN_REQUIRED")
        return kind
    if token:
        raise ValueError("Q16_REFUSE_TO_SEND_GITHUB_TOKEN_OUTSIDE_GITHUB")
    return "presigned_https"


def authorized_headers(url: str, token: str) -> list[str]:
    if request_kind(url, token) == "presigned_https":
        return []
    return [
        "Accept: application/octet-stream",
        "X-GitHub-Api-Version: 2022-11-28",
        f"Authorization: Bearer {token}",
    ]


def select_asset(release: dict, expected_tag: str) -> int:
    """Resolve the immutable binary strictly by name, bytes and checksum."""
    if release.get("tag_name") != expected_tag:
        raise ValueError("Q16_GITHUB_RELEASE_TAG_MISMATCH")
    matches = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name") == ASSET_NAME
    ]
    if len(matches) != 1:
        raise ValueError("Q16_GITHUB_RELEASE_ASSET_NOT_UNIQUE")
    asset = matches[0]
    asset_id = asset.get("id")
    if (
        type(asset_id) is not int
        or asset_id < 1
        or asset.get("state") != "uploaded"
        or asset.get("size") != EXPECTED_BYTES
    ):
        raise ValueError("Q16_GITHUB_RELEASE_ASSET_METADATA_INVALID")
    digest = asset.get("digest")
    if digest is not None and digest != "sha256:" + ARCHIVE_SHA256:
        raise ValueError("Q16_GITHUB_RELEASE_ASSET_DIGEST_MISMATCH")
    return asset_id


def curl_to_file(
    url: str, target: Path, headers: list[str], max_bytes: int
) -> None:
    command = [
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https",
        "--max-redirs", "4", "--connect-timeout", "15", "--max-time", "300",
        "--max-filesize", str(max_bytes), "--retry", "1",
        "--output", str(target),
    ]
    for header in headers:
        command.extend(("--header", header))
    command.append(url)
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=315
    )
    if result.returncode:
        # Never print stdout, stderr, command, signed URL, or bearer token.
        raise ValueError(f"Q16_PRIVATE_CURL_EXIT_{result.returncode}")


def download(destination: Path) -> None:
    url = os.environ.get("Q13_PRIVATE_RELEASE_URL", "").strip()
    token = os.environ.get("Q13_GITHUB_ARCHIVE_TOKEN", "").strip()
    if not url:
        raise ValueError("Q16_PRIVATE_RELEASE_URL_NOT_CONFIGURED")
    kind = request_kind(url, token)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.exists():
        raise ValueError("Q16_DESTINATION_MUST_NOT_EXIST")
    try:
        if kind == "github_tag":
            parsed = urlsplit(url)
            expected_tag = parsed.path.rsplit("/", 1)[1]
            with tempfile.TemporaryDirectory(prefix="q16_tag_metadata_") as temp:
                metadata_path = Path(temp) / "release.json"
                headers = authorized_headers(url, token)
                headers[0] = "Accept: application/vnd.github+json"
                curl_to_file(url, metadata_path, headers, 1_048_576)
                release = json.loads(metadata_path.read_text(encoding="utf-8"))
                asset_id = select_asset(release, expected_tag)
            repo_path = parsed.path.split("/releases/", 1)[0]
            url = (
                "https://api.github.com" + repo_path
                + f"/releases/assets/{asset_id}"
            )
        curl_to_file(url, destination, authorized_headers(url, token), 80_000_000)
        if destination.stat().st_size != EXPECTED_BYTES:
            raise ValueError("Q16_PRIVATE_ARCHIVE_WRONG_SIZE")
        if sha256(destination) != ARCHIVE_SHA256:
            raise ValueError("Q16_PRIVATE_ARCHIVE_SHA256_MISMATCH")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        destination.unlink(missing_ok=True)
        raise
    print("PASS_Q16_PRIVATE_HTTPS_ARCHIVE_EXACT_SIZE_AND_SHA256")
    print("PASS_Q16_NO_HTTP_OR_UNAUTHORIZED_TOKEN_FORWARDING")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        download(args.output)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
        if reason.startswith("Q16_"):
            raise SystemExit(reason) from None
        raise SystemExit("Q16_PRIVATE_FETCH_FAILED_NO_SECRET_LOGGING") from None


if __name__ == "__main__":
    main()
