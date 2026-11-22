#!/usr/bin/env python3
"""Safely fetch the immutable Q7 archive from HTTPS or a private GitHub Release.

Never prints the URL or token, never sends a GitHub token to a non-GitHub
host, never permits HTTP (including redirected HTTP), and refuses any object
whose size or SHA-256 differs from the frozen private Q7 candidate.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from q13_private_fixture_gate import ARCHIVE_SHA256, sha256

EXPECTED_BYTES = 61_656_622
GITHUB_ASSET_PATH = re.compile(
    r"/repos/minseogiiiii/[A-Za-z0-9_.-]+/releases/assets/[1-9][0-9]*"
)


def authorized_headers(url: str, token: str) -> list[str]:
    """Allow bearer auth only for an exact GitHub Releases asset API endpoint."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Q16_PRIVATE_ARCHIVE_URL_MUST_BE_HTTPS")
    if parsed.fragment:
        raise ValueError("Q16_PRIVATE_ARCHIVE_FRAGMENT_DISALLOWED")
    if parsed.hostname == "api.github.com":
        if (
            parsed.port is not None
            or parsed.query
            or not GITHUB_ASSET_PATH.fullmatch(parsed.path)
        ):
            raise ValueError("Q16_GITHUB_RELEASE_ASSET_ENDPOINT_REQUIRED")
        if not token:
            raise ValueError("Q16_GITHUB_READONLY_ARCHIVE_TOKEN_REQUIRED")
        return [
            "Accept: application/octet-stream",
            "X-GitHub-Api-Version: 2022-11-28",
            f"Authorization: Bearer {token}",
        ]
    if token:
        raise ValueError("Q16_REFUSE_TO_SEND_GITHUB_TOKEN_OUTSIDE_GITHUB")
    return []


def download(destination: Path) -> None:
    url = os.environ.get("Q13_PRIVATE_RELEASE_URL", "").strip()
    token = os.environ.get("Q13_GITHUB_ARCHIVE_TOKEN", "").strip()
    if not url:
        raise ValueError("Q16_PRIVATE_RELEASE_URL_NOT_CONFIGURED")
    headers = authorized_headers(url, token)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.exists():
        raise ValueError("Q16_DESTINATION_MUST_NOT_EXIST")
    command = [
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https",
        "--max-redirs", "4", "--connect-timeout", "15", "--max-time", "300",
        "--max-filesize", "80000000", "--retry", "1", "--output",
        str(destination),
    ]
    for header in headers:
        command.extend(("--header", header))
    command.append(url)
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=315
        )
        if result.returncode:
            raise ValueError(
                f"Q16_SECURE_PRIVATE_ARCHIVE_DOWNLOAD_FAILED_CURL_{result.returncode}"
            )
        if destination.stat().st_size != EXPECTED_BYTES:
            raise ValueError("Q16_PRIVATE_ARCHIVE_WRONG_SIZE")
        if sha256(destination) != ARCHIVE_SHA256:
            raise ValueError("Q16_PRIVATE_ARCHIVE_SHA256_MISMATCH")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        destination.unlink(missing_ok=True)
        raise
    print("PASS_Q16_AUTHORIZED_PRIVATE_HTTPS_ARCHIVE_EXACT_SHA256")
    print("PASS_Q16_ARCHIVE_DOWNLOAD_PRIVATE_NO_PLAINTEXT_FALLBACK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        download(args.output)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # Never echo urllib URL, curl command, stdout, stderr, or secret.
        reason = str(exc)
        if reason.startswith("Q16_"):
            raise SystemExit(reason) from None
        raise SystemExit("Q16_PRIVATE_FETCH_FAILED_WITHOUT_LOGGING_SECRETS") from None


if __name__ == "__main__":
    main()
