"""v32 fail-closed asset-pin gate for the verified historical React/Funnest build.

This is a *local software integrity* control, not code signing, secure boot,
venue attestation, or a defense against an attacker able to edit Python code.
All three build files are hashed on each request so a modified sibling asset
cannot silently be served through an unchanged index page.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
MANIFEST = BASE / "frontend" / "v32_dist_integrity.json"
DIST = BASE / "frontend" / "dist_v32"
MANIFEST_BYTES_MAX = 4096
PINNED_MANIFEST_SHA256 = (
    "299867712342f65379fcee36f8bd7c7249ad16813ab74a41e9eebe30bccca750"
)


class FrontendIntegrityError(ValueError):
    pass


def _read_regular(path: Path, max_bytes: int) -> bytes:
    if path.is_symlink() or path.parent.is_symlink():
        raise FrontendIntegrityError("Symlinked frontend build file or folder")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise FrontendIntegrityError("Pinned frontend build file missing") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size < 1 or st.st_size > max_bytes:
            raise FrontendIntegrityError(
                "Unacceptable frontend build file size or type"
            )
        with os.fdopen(os.dup(fd), "rb") as fp:
            content = fp.read(max_bytes + 1)
        if len(content) != st.st_size:
            raise FrontendIntegrityError("Frontend build changed while reading")
        return content
    finally:
        os.close(fd)


def verify_build(
    dist: Path = DIST,
    manifest_path: Path = MANIFEST,
    pinned_sha256: str = PINNED_MANIFEST_SHA256,
) -> tuple[dict[str, bytes], dict]:
    """Return *only* locally pinned exact bytes to serve, or reject all."""
    if dist.is_symlink() or manifest_path.parent.is_symlink():
        raise FrontendIntegrityError("Unsafe frontend build directory")
    raw = _read_regular(manifest_path, MANIFEST_BYTES_MAX)
    if hashlib.sha256(raw).hexdigest() != pinned_sha256:
        raise FrontendIntegrityError(
            "Frontend integrity manifest not pinned to this release"
        )

    def no_dup(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FrontendIntegrityError("Duplicate integrity manifest key")
            result[key] = value
        return result

    try:
        manifest = json.loads(
            raw,
            object_pairs_hook=no_dup,
            parse_constant=lambda _: (_ for _ in ()).throw(
                FrontendIntegrityError("Invalid JSON constant")
            ),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise FrontendIntegrityError("Invalid pinned frontend manifest") from exc
    files = manifest.get("files")
    if (
        manifest.get("schema") != "v32_funnest_build_integrity_v1"
        or not isinstance(files, dict)
        or len(files) != 3
    ):
        raise FrontendIntegrityError("Unsupported pinned frontend build inventory")
    expected_paths = {"index.html"}
    for name in files:
        if re.fullmatch(r"assets/index-[A-Za-z0-9_-]+\.(?:js|css)", name):
            expected_paths.add(name)
    if len(expected_paths) != 3 or set(files) != expected_paths:
        raise FrontendIntegrityError("Frontend asset inventory or filename invalid")
    if {x.rsplit(".", 1)[-1] for x in files if x != "index.html"} != {"js", "css"}:
        raise FrontendIntegrityError("Frontend requires one JS and one CSS bundle")
    observed: dict[str, bytes] = {}
    for name, desc in files.items():
        if not isinstance(desc, dict) or set(desc) != {"sha256", "bytes"}:
            raise FrontendIntegrityError("Invalid frontend asset descriptor")
        limit = (
            50_000
            if name == "index.html"
            else (8_000_000 if name.endswith(".js") else 250_000)
        )
        data = _read_regular(dist / name, limit)
        if (
            type(desc["bytes"]) is not int
            or desc["bytes"] != len(data)
            or not isinstance(desc["sha256"], str)
            or hashlib.sha256(data).hexdigest() != desc["sha256"]
        ):
            raise FrontendIntegrityError("Frontend asset hash/length mismatch")
        observed[name] = data
    index = observed["index.html"]
    referenced = set(
        m.decode("ascii")
        for m in re.findall(rb"/((?:assets/index-[A-Za-z0-9_-]+\.(?:js|css)))", index)
    )
    if referenced != expected_paths - {"index.html"}:
        raise FrontendIntegrityError(
            "Frontend index references files outside pinned release"
        )
    return observed, manifest


def get_asset(
    path: str,
    *,
    dist: Path = DIST,
    manifest_path: Path = MANIFEST,
    pinned_sha256: str = PINNED_MANIFEST_SHA256,
) -> tuple[bytes, str]:
    known, _ = verify_build(dist, manifest_path, pinned_sha256)
    if path in ("/research", "/research/index.html"):
        return known["index.html"], "text/html"
    if path.startswith("/assets/"):
        name = path.removeprefix("/")
        if name in known:
            return known[name], "application/javascript" if name.endswith(
                ".js"
            ) else "text/css"
    raise FrontendIntegrityError("Frontend file is not part of the pinned build")
