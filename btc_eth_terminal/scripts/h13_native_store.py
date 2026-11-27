"""Private persistent native archive acquisition and admission.

One writer on a local POSIX filesystem. Raw and derived files published together.
Failed attempts remain in incoming/ and are never cataloged. No remote upload.
"""

from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from h1_streaming_parquet import convert, digest
from h4_cross_session_catalog import catalog, inspect
from v36_3_durable_archive_gate import audit, BOUNDS
from independent_soak_replay import strict_json

ROOT = Path(__file__).resolve().parents[1]
RESERVE = 4 * 1024**3


def need(ok, why):
    if not ok:
        raise ValueError("H13_" + why)


def pin_ok(s):
    return isinstance(s, str) and re.fullmatch(r"[a-f0-9]{64}", s) is not None


def write_json(path, value):
    with path.open("x") as f:
        json.dump(value, f, sort_keys=True, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def read_json(path, limit=1_000_000):
    need(
        path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= limit,
        "JSON_FILE",
    )
    return strict_json(path.read_bytes())


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_tree(root):
    for parent, dirs, files in os.walk(root, topdown=False):
        for name in files:
            p = Path(parent) / name
            need(not p.is_symlink() and p.is_file(), "UNSAFE_PUBLICATION")
            with p.open("rb") as f:
                os.fsync(f.fileno())
        for name in dirs:
            need(not (Path(parent) / name).is_symlink(), "UNSAFE_PUBLICATION")
        fsync_dir(parent)


def private_dir(p):
    s = p.lstat()
    need(
        stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid() and s.st_mode & 0o077 == 0,
        "PRIVATE_DIRECTORY",
    )


def init(root):
    root = Path(root)
    if not root.exists():
        root.mkdir(mode=0o700)
    private_dir(root)
    for n in ("incoming", "sessions"):
        p = root / n
        if not p.exists():
            p.mkdir(mode=0o700)
        private_dir(p)
    return root


@contextmanager
def locked(root):
    root = init(root)
    fd = os.open(root / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        need(
            stat.S_ISREG(st.st_mode)
            and st.st_uid == os.getuid()
            and st.st_nlink == 1
            and st.st_mode & 0o077 == 0,
            "LOCK_FILE",
        )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def entries(root, *, fixture=False):
    out = []
    for folder in sorted((Path(root) / "sessions").iterdir()):
        need(
            folder.is_dir() and not folder.is_symlink() and pin_ok(folder.name),
            "SESSION_DIRECTORY",
        )
        r = read_json(folder / "admission.json")
        need(
            r["schema"] == "h13_native_admission_v1"
            and r["source_manifest_sha256"] == folder.name
            and r["source_kind"]
            == ("SYNTHETIC_TEST_ONLY" if fixture else "ARCHIVE_REPLAY")
            and pin_ok(r["dataset_receipt_sha256"])
            and r["execution_authorized"] is False,
            "SESSION_RECEIPT",
        )
        need(
            digest(folder / "raw" / "manifest.json") == folder.name, "RAW_MANIFEST_PIN"
        )
        out.append(
            {
                "dataset_path": str(folder / "dataset"),
                "dataset_receipt_sha256": r["dataset_receipt_sha256"],
                "role": "DEVELOPMENT",
            }
        )
    return out


def inventory(root, *, fixture=False, reaudit=False):
    with locked(root) as root:
        es = entries(root, fixture=fixture)
        c = catalog(es, 0, fixture=fixture) if es else None
        audits = []
        if reaudit and c:
            for row in c["sessions"]:
                pin = row["source_manifest_sha256"]
                folder = root / "sessions" / pin
                a = audit(folder / "raw", pin)
                r = read_json(folder / "admission.json")
                need(
                    (
                        not a["eligible_for_archived_replay"]
                        and not a["partial_chunks_not_admitted"]
                        and not a["manifest_tmp_not_admitted"]
                    )
                    if fixture
                    else a["eligible_for_archived_replay"],
                    "RAW_REAUDIT",
                )
                need(
                    a["sealed_messages_verified"]
                    == row["source_messages"]
                    == r["source_messages"]
                    and a["sealed_bytes_verified"] == r["archive_bytes"]
                    and row["first_receiver_ms"] == r["first_receiver_ms"]
                    and row["last_receiver_ms"] == r["last_receiver_ms"],
                    "RAW_DATASET_INVENTORY",
                )
                audits.append(
                    {
                        "source_manifest_sha256": pin,
                        "sealed_messages": a["sealed_messages_verified"],
                        "sealed_bytes": a["sealed_bytes_verified"],
                        "classification": a["classification"],
                    }
                )
        return {
            "schema": "h13_local_inventory_v1",
            "sessions": es,
            "catalog": c,
            "raw_audits": audits,
            "unpublished_attempts": len(list((root / "incoming").iterdir())),
            "raw_chunks_reaudited": bool(reaudit and c),
            "external_attestation": False,
            "execution_authorized": False,
        }


def publish(root, stage, pin, origin, *, fixture=False):
    raw = stage / "raw"
    dest = root / "sessions" / pin
    need(pin_ok(pin) and not dest.exists(), "DUPLICATE_SOURCE")
    report = convert(raw, pin, stage / "dataset", fixture=fixture)
    dp = digest(stage / "dataset" / "dataset.json")
    new = {
        "dataset_path": str(stage / "dataset"),
        "dataset_receipt_sha256": dp,
        "role": "DEVELOPMENT",
    }
    c = catalog(entries(root, fixture=fixture) + [new], 0, fixture=fixture)
    row = next(x for x in c["sessions"] if x["source_manifest_sha256"] == pin)
    r = {
        "schema": "h13_native_admission_v1",
        "source_manifest_sha256": pin,
        "dataset_receipt_sha256": dp,
        "origin": origin,
        "source_kind": report["source_kind"],
        "source_messages": row["source_messages"],
        "first_receiver_ms": row["first_receiver_ms"],
        "last_receiver_ms": row["last_receiver_ms"],
        "archive_bytes": sum(x.stat().st_size for x in (raw / "chunks").iterdir()),
        "derived_bytes": sum(
            x.stat().st_size for x in (stage / "dataset").glob("*.parquet")
        ),
        "catalog_sha256_at_publication": c["catalog_sha256"],
        "independent_replay_parity": True,
        "external_attestation": False,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }
    write_json(stage / "admission.json", r)
    durable_tree(stage)
    need(not dest.exists(), "DUPLICATE_SOURCE")
    os.rename(stage, dest)
    fsync_dir(root / "sessions")
    fsync_dir(root / "incoming")
    return r


def import_archive(root, source, pin, *, fixture=False):
    """Copy only a complete audited archive; source directory untouched."""
    source = Path(source)
    with locked(root) as root:
        need(pin_ok(pin) and not (root / "sessions" / pin).exists(), "DUPLICATE_OR_PIN")
        cert = audit(source, pin)
        mf = source / "manifest.json"
        need(
            mf.is_file()
            and not mf.is_symlink()
            and 0 < mf.stat().st_size <= BOUNDS["MAX_MANIFEST_BYTES"],
            "MANIFEST_FILE",
        )
        with mf.open("rb") as f:
            manifest = f.read(BOUNDS["MAX_MANIFEST_BYTES"] + 1)
        need(
            len(manifest) <= BOUNDS["MAX_MANIFEST_BYTES"]
            and hashlib.sha256(manifest).hexdigest() == pin,
            "MANIFEST_CHANGED",
        )
        m = strict_json(manifest)
        need(
            (
                m.get("synthetic_fixture") is True
                and not cert["partial_chunks_not_admitted"]
                and not cert["manifest_tmp_not_admitted"]
            )
            if fixture
            else cert["eligible_for_archived_replay"],
            "SOURCE_ADMISSION",
        )
        need(
            shutil.disk_usage(root).free >= RESERVE + 4 * cert["sealed_bytes_verified"],
            "DISK",
        )
        stage = Path(tempfile.mkdtemp(prefix="import-", dir=root / "incoming"))
        raw = stage / "raw"
        raw.mkdir()
        (raw / "chunks").mkdir()
        (raw / "manifest.json").write_bytes(manifest)
        for ch in m["chunks"]:
            name = ch["file"]
            size = ch["archived_line_bytes"]
            need(
                re.fullmatch(r"chunks/chunk_\d{5}_conn_\d{3}\.jsonl", name)
                and type(size) is int
                and 0 < size <= BOUNDS["MAX_CHUNK_BYTES"],
                "CHUNK_COPY_CONTRACT",
            )
            fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as src, (raw / name).open("xb") as dst:
                st = os.fstat(src.fileno())
                need(
                    stat.S_ISREG(st.st_mode)
                    and st.st_nlink == 1
                    and st.st_size == size,
                    "CHUNK_COPY_FILE",
                )
                h = hashlib.sha256()
                n = 0
                while block := src.read(1024 * 1024):
                    n += len(block)
                    need(n <= size, "CHUNK_COPY_LIMIT")
                    h.update(block)
                    dst.write(block)
                need(
                    n == size and h.hexdigest() == ch["archive_sha256"],
                    "CHUNK_COPY_HASH",
                )
        # Re-audited copied bytes by H1; never publish incomplete source.
        return publish(root, stage, pin, "EXTERNALLY_PINNED_IMPORT", fixture=fixture)


def capture(root, duration, budget):
    need(
        type(duration) is int
        and 60 <= duration <= 3600
        and type(budget) is int
        and 50_000_000 <= budget <= 8_000_000_000,
        "CAPTURE_BOUNDS",
    )
    with locked(root) as root:
        need(shutil.disk_usage(root).free >= RESERVE + 4 * budget, "DISK")
        node = shutil.which("node")
        need(node is not None, "NODE")
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        need(
            re.fullmatch(r"v\d+\.\d+\.\d+", version)
            and int(version[1:].split(".")[0]) >= 22,
            "NODE_VERSION",
        )
        stage = Path(tempfile.mkdtemp(prefix="capture-", dir=root / "incoming"))
        write_json(
            stage / "request.json",
            {
                "duration_seconds": duration,
                "archive_budget_bytes": budget,
                "collector_sha256": digest(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
                "public_read_only": True,
            },
        )
        env = {
            "PATH": os.environ["PATH"],
            "SOAK_DURATION_SECONDS": str(duration),
            "SOAK_RAW_BUDGET_BYTES": str(budget),
            "SOAK_ARCHIVE_BUDGET_BYTES": str(budget),
            "SOAK_DISK_RESERVE_BYTES": str(RESERVE),
            "SOAK_CHUNK_LIMIT_BYTES": "8000000",
        }
        with (stage / "collector.log").open("xb") as log:
            proc = subprocess.run(
                [node, str(ROOT / "scripts/btc_eth_multiday_v20.mjs")],
                cwd=stage,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=duration + 90,
            )
        need(proc.returncode == 0, "CAPTURE_NOT_CLEAN")
        captures = list((stage / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        need(
            len(captures) == 1
            and captures[0].is_dir()
            and not captures[0].is_symlink(),
            "CAPTURE_INVENTORY",
        )
        os.rename(captures[0], stage / "raw")
        (stage / "soak_evidence").rmdir()
        raw = stage / "raw"
        m = read_json(raw / "manifest.json", BOUNDS["MAX_MANIFEST_BYTES"])
        need(
            m.get("synthetic_fixture") is False
            and m.get("required_duration_seconds") == duration
            and m.get("finish_reason") == "DURATION_REACHED"
            and m.get("total_reconnects") == 0,
            "CAPTURE_CONTRACT",
        )
        return publish(
            root, stage, digest(raw / "manifest.json"), "LOCAL_PUBLIC_CAPTURE"
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    sub = p.add_subparsers(dest="action", required=True)
    q = sub.add_parser("capture")
    q.add_argument("--duration", type=int, default=1800)
    q.add_argument("--archive-budget-bytes", type=int, default=1_000_000_000)
    q = sub.add_parser("import")
    q.add_argument("--archive", type=Path, required=True)
    q.add_argument("--manifest-sha256", required=True)
    q = sub.add_parser("inventory")
    q.add_argument("--reaudit-raw", action="store_true")
    a = p.parse_args()
    if a.action == "capture":
        r = capture(a.root, a.duration, a.archive_budget_bytes)
    elif a.action == "import":
        r = import_archive(a.root, a.archive, a.manifest_sha256)
    else:
        r = inventory(a.root, reaudit=a.reaudit_raw)
    print(json.dumps(r, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
