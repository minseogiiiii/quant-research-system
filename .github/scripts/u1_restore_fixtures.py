"""Restore only authenticated frozen Q7 evidence; never replace candidate source."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from q13_private_fixture_gate import PREFIX, verify_zip

DATA_ROOTS = ("backend/data/", "examples/", "evidence/", "verification/",
              "reference/", "reports/")
DATA_SUFFIXES = {".json", ".jsonl", ".csv", ".txt", ".md", ".log",
                 ".exit", ".zip", ".gz", ".sha256", ".html"}
DIST_ROOTS = ("frontend/dist/", "frontend/dist_v32/", "frontend/dist_v33/",
              "frontend/dist_v35_1/", "frontend/dist_v36/", "frontend/dist_v37/")
DIST_SUFFIXES = {".html", ".css", ".js"}


def allowed(rel: str) -> bool:
    p = Path(rel)
    if any(part.startswith(".") or part == "node_modules" for part in p.parts):
        return False
    if rel.startswith(DIST_ROOTS):
        return p.suffix in DIST_SUFFIXES
    if rel.startswith(DATA_ROOTS):
        return p.suffix in DATA_SUFFIXES
    return len(p.parts) == 1 and p.suffix in DATA_SUFFIXES


def restore(archive: Path, destination: Path) -> dict:
    root = destination.resolve(strict=True)
    restored = existing = 0
    with verify_zip(archive) as z:
        for entry in z.infolist():
            if entry.is_dir():
                continue
            rel = entry.filename[len(PREFIX):]
            if not allowed(rel):
                continue
            target = root / rel
            if not target.resolve().is_relative_to(root):
                raise ValueError("FIXTURE_PATH_ESCAPE")
            if any(p.is_symlink() for p in (target, *target.parents)
                   if p != root and p.is_relative_to(root)):
                raise ValueError("FIXTURE_SYMLINK")
            if target.exists():
                if not target.is_file():
                    raise ValueError("FIXTURE_NONFILE_COLLISION")
                existing += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as source, target.open("xb") as out:
                shutil.copyfileobj(source, out)
            restored += 1
    return {"restored": restored, "existing_not_overwritten": existing,
            "frozen_zip_verified": True, "candidate_source_overwritten": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(restore(args.archive, args.destination), sort_keys=True))
