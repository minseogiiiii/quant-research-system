#!/usr/bin/env python3
"""Verify eight staged Q7 source blobs without implying release readiness."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from pathlib import Path

BASE = "64aa85081cab73c917333249eb8e200c8d4044fa"
BRANCH = "staging/q7-readonly-qualification-20261119"
PINS = {
    "btc_eth_terminal/backend/v37_factor_lab.py":
        "a6d49d3c972d33d978f7a4373e11cd5eef1c1094",
    "btc_eth_terminal/backend/v37_frontend_integrity.py":
        "b7cb343a4f921b9a2db110feff2090aa67294f3b",
    "btc_eth_terminal/backend/v37_independent_factor_audit.py":
        "b8c32b0406492b63a0c87efbaa025682dfe5a26e",
    "btc_eth_terminal/backend/q7_experiment_prereg.json":
        "e498cfaa0aec66b24e3720599566a4babcdade67",
    "btc_eth_terminal/backend/q7_experiment_prereg.sha256.json":
        "e3445a4eb19e74c4abd1d69e5a02cfabc6bde92a",
    "btc_eth_terminal/frontend/src/FactorLabPanel.tsx":
        "4e2c9347dd90f2e9748eec52602ee250d228a261",
    "btc_eth_terminal/frontend/v37_dist_integrity.json":
        "e4b28a72ef15f772271236f7e047af4757fcc303",
    "btc_eth_terminal/scripts/v37_build_frontend.py":
        "bbd825e35f02f3d653a1b5de7e5ef7f4062edaaa",
}


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], check=True, text=True,
        capture_output=True,
    )
    return proc.stdout.strip()


def verify(root: Path) -> dict:
    require(git("rev-parse", "--is-inside-work-tree") == "true", "NOT_GIT")
    branch = (
        git("branch", "--show-current")
        or os.environ.get("GITHUB_HEAD_REF", "")
        or os.environ.get("GITHUB_REF_NAME", "")
    )
    require(branch == BRANCH, "WRONG_BRANCH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository:
        require(
            repository == "minseogiiiii/quant-research-system", "WRONG_REPO"
        )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE, "HEAD"], check=False
    )
    require(ancestor.returncode == 0, "BASELINE_NOT_ANCESTOR")
    require(not git("status", "--porcelain"), "WORKTREE_DIRTY")

    for path, expected in PINS.items():
        source = root / path
        require(source.is_file() and not source.is_symlink(), "MISSING_SOURCE")
        raw = source.read_bytes()
        blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        require(blob == expected, "GIT_BLOB_MISMATCH:" + path)
        require(git("rev-parse", "HEAD:" + path) == expected, "HEAD_MISMATCH")
        if path.endswith(".py"):
            ast.parse(raw, filename=path)

    backend = root / "btc_eth_terminal" / "backend"
    prereg = (backend / "q7_experiment_prereg.json").read_bytes()
    pin = json.loads((backend / "q7_experiment_prereg.sha256.json").read_bytes())
    research = json.loads(prereg)
    require(
        hashlib.sha256(prereg).hexdigest() == pin["preregistration_sha256"],
        "PREREG_PIN_MISMATCH",
    )
    require(research["orders_enabled"] is False, "ORDERS_ACTIVE")
    require(research["split"]["blind_final"] is None, "BLIND_OOS_OPENED")
    require(research["current_verdict"] == "INSUFFICIENT_EVIDENCE", "FALSE_ALPHA")

    frontend = root / "btc_eth_terminal" / "frontend"
    manifest = (frontend / "v37_dist_integrity.json").read_bytes()
    integrity_module = (backend / "v37_frontend_integrity.py").read_text()
    require(
        hashlib.sha256(manifest).hexdigest() in integrity_module,
        "FRONTEND_MANIFEST_PIN_MISMATCH",
    )
    manifest_obj = json.loads(manifest)
    require(manifest_obj["trading_enabled"] is False, "ASSET_TRADING_ACTIVE")
    require(len(manifest_obj["files"]) == 3, "UNEXPECTED_ASSET_COUNT")

    changed = git("diff", "--name-only", BASE + "..HEAD").splitlines()
    banned_parts = {
        "data", "dist", "dist_v36", "dist_v37", "node_modules",
        "__pycache__", ".env",
    }
    banned_extensions = {
        ".jsonl", ".gz", ".zip", ".key", ".pem", ".sqlite", ".db",
    }
    for path in changed:
        source_path = Path(path)
        require(not set(source_path.parts) & banned_parts, "PRIVATE_PATH:" + path)
        require(
            source_path.suffix not in banned_extensions,
            "PRIVATE_FILE:" + path,
        )
    return {
        "status": "PASS_PARTIAL_SOURCE_NOT_RELEASE_APPROVED",
        "git_pinned_q7_files": len(PINS),
        "q7_expected_public_source_files": 278,
        "fully_integrated": False,
        "real_q7_live_verified": False,
        "orders_enabled": False,
    }


if __name__ == "__main__":
    print(json.dumps(verify(Path.cwd()), indent=2, sort_keys=True))
