"""Bounded large-capture gate, reusing the unchanged v35 raw-byte auditor.

Never promotes crash fragments, faulted/synthetic sources or reconnects to
continuous market truth. No source mutation, no LIVE or execution admission.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import sys
from hashlib import sha256
from pathlib import Path

BOUNDS = {
    "MAX_MANIFEST_BYTES": 24_000_000,
    "MAX_CHUNKS": 20_000,
    "MAX_CHUNK_BYTES": 32_000_000,
    "MAX_LINE_BYTES": 18_000_000,
    "MAX_MESSAGES": 30_000_000,
    "MAX_TOTAL_BYTES": 80_000_000_000,
}
SHA = re.compile(r"[0-9a-f]{64}\Z")


class DurableRejected(ValueError):
    pass


def _gate():
    # Isolated import means frozen v35 limits never change for its callers.
    source = Path(__file__).resolve().parents[1] / "backend/v35_archive_ingress.py"
    spec = importlib.util.spec_from_file_location(
        "_v36_durable_isolated_ingress", source
    )
    if not spec or not spec.loader:
        raise DurableRejected("ORIGINAL_VERIFIER_NOT_AVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in BOUNDS.items():
        setattr(module, key, value)
    return module


def _file_identity(path: Path) -> tuple:
    """Fail closed on hardlinks, symlinks and mutable source entry metadata."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DurableRejected("UNSAFE_LINKED_OR_NONREGULAR_SOURCE")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _dir_identity(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise DurableRejected("UNSAFE_SOURCE_DIRECTORY")
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def _sha(path, limit):
    """Hash an O_NOFOLLOW fd and verify inode did not change while reading."""
    before = _file_identity(path)
    if not 0 < before[2] <= limit:
        raise DurableRejected("UNSAFE_OR_UNBOUNDED_SOURCE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (before[0], before[1])
        ):
            raise DurableRejected("SOURCE_PATH_REPLACED_DURING_OPEN")
        h = sha256()
        n = 0
        with os.fdopen(fd, "rb", closefd=False) as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                n += len(block)
                if n > limit:
                    raise DurableRejected("SOURCE_SIZE_EXCEEDED_BOUND")
                h.update(block)
        if n != before[2] or _file_identity(path) != before:
            raise DurableRejected("SOURCE_MUTATED_DURING_HASH")
        return h.hexdigest()
    finally:
        os.close(fd)


def _mono_span(root, entries):
    if not entries:
        return None

    def n(path, first):
        with path.open("rb") as f:
            if first:
                line = f.readline(BOUNDS["MAX_LINE_BYTES"] + 1)
            else:
                data = f.read(BOUNDS["MAX_CHUNK_BYTES"] + 1)
                if len(data) > BOUNDS["MAX_CHUNK_BYTES"]:
                    raise DurableRejected("UNBOUNDED_FINAL_CHUNK")
                line = data.splitlines()[-1]
        try:
            val = json.loads(line)["local_monotonic_ns"]
        except (ValueError, KeyError, TypeError, IndexError):
            raise DurableRejected("MISSING_MONOTONIC_SOURCE_EVIDENCE")
        if (
            not isinstance(val, str)
            or not val.isascii()
            or not val.isdigit()
            or len(val) > 32
        ):
            raise DurableRejected("BAD_MONOTONIC_SOURCE_EVIDENCE")
        return int(val)

    return (
        n(root / entries[-1]["file"], False) - n(root / entries[0]["file"], True)
    ) / 1e9


def audit(root: Path, pin: str) -> dict:
    root = Path(root)
    root_id = _dir_identity(root)
    if not isinstance(pin, str) or not SHA.fullmatch(pin):
        raise DurableRejected("EXTERNAL_SHA256_REQUIRED")
    manifest_path = root / "manifest.json"
    manifest_id = _file_identity(manifest_path)
    digest = _sha(manifest_path, BOUNDS["MAX_MANIFEST_BYTES"])
    if digest != pin:
        raise DurableRejected("MANIFEST_PIN_MISMATCH")
    try:
        manifest = json.loads((root / "manifest.json").read_bytes())
    except (ValueError, UnicodeError):
        raise DurableRejected("INVALID_MANIFEST")
    if not isinstance(manifest, dict):
        raise DurableRejected("INVALID_MANIFEST_OBJECT")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or len(chunks) > BOUNDS["MAX_CHUNKS"]:
        raise DurableRejected("UNBOUNDED_CHUNK_INVENTORY")
    directory = root / "chunks"
    directory_id = _dir_identity(directory)
    pending = []
    committed = []
    for p in directory.iterdir():
        if p.is_symlink() or not p.is_file():
            raise DurableRejected("UNSAFE_CHUNK_ENTRY")
        if p.name.endswith(".jsonl.partial"):
            pending.append(p.name)
        elif p.name.endswith(".jsonl"):
            committed.append(p.name)
        else:
            raise DurableRejected("UNEXPECTED_CHUNK_ENTRY")
    listed = {entry.get("file") for entry in chunks if isinstance(entry, dict)}
    if len(listed) != len(chunks):
        raise DurableRejected("DUPLICATE_MANIFEST_CHUNK")
    if any("chunks/" + n not in listed for n in committed):
        raise DurableRejected("UNLISTED_FINALIZED_SOURCE_CHUNK")
    temporary = (root / "manifest.json.tmp").exists()
    if (root / "manifest.json.tmp").is_symlink():
        raise DurableRejected("UNSAFE_PENDING_MANIFEST")
    # Prevent mutable hardlinks and detect source substitution/changes between
    # v35's first hash pass and its second semantic read. v35 remains frozen.
    chunk_ids = {}
    for entry in chunks:
        if not isinstance(entry, dict):
            raise DurableRejected("INVALID_CHUNK_ENTRY")
        name = entry.get("file")
        if not isinstance(name, str) or not re.fullmatch(
            r"chunks/chunk_\d{5}_conn_\d{3}\.jsonl", name
        ):
            raise DurableRejected("UNSAFE_CHUNK_NAME")
        chunk_ids[name] = _file_identity(root / name)
    # On SIGKILL an active .partial has no finalized chunk manifest entry.
    # Forensic classification must not silently ingest this source.
    cert = None
    if chunks:
        verifier = _gate()
        try:
            cert = verifier.verify_capture(root, pin)
        except (verifier.ArchiveRejected, OSError) as exc:
            raise DurableRejected(str(exc))
    elif committed:
        raise DurableRejected("FINALIZED_CHUNKS_NOT_MANIFESTED")
    span = _mono_span(root, chunks) if chunks else None
    # A second externally-pinned digest after semantic verification makes
    # ordinary concurrent changes fail closed. This still cannot defeat an
    # adversary with write access who can temporarily swap-and-restore input;
    # certification therefore requires a frozen, permission-isolated source.
    if (
        _file_identity(manifest_path) != manifest_id
        or _sha(manifest_path, BOUNDS["MAX_MANIFEST_BYTES"]) != pin
    ):
        raise DurableRejected("MANIFEST_CHANGED_DURING_AUDIT")
    for entry in chunks:
        name = entry["file"]
        if (
            _file_identity(root / name) != chunk_ids[name]
            or _sha(root / name, BOUNDS["MAX_CHUNK_BYTES"]) != entry["archive_sha256"]
        ):
            raise DurableRejected("CHUNK_CHANGED_DURING_AUDIT")
    if _dir_identity(directory) != directory_id or _dir_identity(root) != root_id:
        raise DurableRejected("SOURCE_DIRECTORY_CHANGED_DURING_AUDIT")
    requested = manifest.get("required_duration_seconds")
    duration_valid = (
        type(requested) is int
        and 15 <= requested <= 86400
        and span is not None
        and span >= requested - 5
    )
    clean = (
        cert is not None
        and cert["source_eligible_for_state_replay"] is True
        and not pending
        and not temporary
        and duration_valid
        and manifest.get("synthetic_fixture") is not True
        and manifest.get("synthetic_fixture_never_admit") is not True
        and manifest.get("controlled_fault_injection") is None
        and manifest.get("injection_not_triggered") is not True
        and manifest.get("finish_reason") == "DURATION_REACHED"
        and manifest.get("total_reconnects") == 0
        and cert["connections"] == 1
    )
    return {
        "schema": "v36_3_durable_archive_gate_v1",
        "classification": "VERIFIED_CLEAN_ARCHIVE_ENVELOPE"
        if clean
        else "QUARANTINED_SOURCE_NO_PROMOTION",
        "manifest_sha256": digest,
        "sealed_chunks_verified": cert["chunks"] if cert else 0,
        "sealed_messages_verified": cert["messages"] if cert else 0,
        "sealed_bytes_verified": cert["archived_bytes"] if cert else 0,
        "partial_chunks_not_admitted": len(pending),
        "manifest_tmp_not_admitted": temporary,
        "source_connections": cert["connections"]
        if cert
        else len(manifest.get("connections", [])),
        "monotonic_source_span_seconds": round(span, 3) if span is not None else None,
        "capture_duration_requested_seconds": requested,
        "eligible_for_archived_replay": clean,
        "independent_exchange_attestation": False,
        "actual_persistent_24h_host_certified": False,
        "live_feed": False,
        "trading_enabled": False,
        "execution_authorized": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--manifest-sha256", required=True)
    args = p.parse_args()
    try:
        result = audit(args.capture, args.manifest_sha256)
    except (DurableRejected, OSError, TypeError, KeyError) as exc:
        print(
            "REJECTED_DURABLE_ARCHIVE "
            + json.dumps({"reason": str(exc), "trading_enabled": False})
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["eligible_for_archived_replay"] else 1


if __name__ == "__main__":
    sys.exit(main())
