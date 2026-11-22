#!/usr/bin/env python3
"""Compare full-repository Ruff errors without counting line movement as regressions.

Diagnostic only: never substitutes for full Validation or release acceptance.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


def fingerprint(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        item["path"],
        item["code"],
        item["message"],
        item["source_line"],
    )


def collect_ruff(root: Path) -> list[dict[str, Any]]:
    root = root.resolve(strict=True)
    run = subprocess.run(
        ["ruff", "check", ".", "--output-format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode not in (0, 1):
        raise RuntimeError(f"Ruff execution failed at {root}: {run.stderr[:1200]}")
    try:
        issues = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ruff returned invalid JSON") from exc
    if not isinstance(issues, list):
        raise RuntimeError("Unexpected Ruff JSON result")
    cache: dict[Path, list[str]] = {}
    output = []
    for issue in issues:
        filename = Path(issue["filename"]).resolve(strict=True)
        try:
            rel = filename.relative_to(root).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"Ruff reported out-of-root path: {filename}") from exc
        if filename not in cache:
            cache[filename] = filename.read_text(encoding="utf-8").splitlines()
        row = issue["location"]["row"]
        lines = cache[filename]
        if not isinstance(row, int) or row < 1 or row > len(lines):
            raise RuntimeError(f"Invalid Ruff source row {rel}:{row}")
        output.append(
            {
                "path": rel,
                "code": issue["code"],
                "message": issue["message"],
                "source_line": lines[row - 1],
                "row": row,
            }
        )
    return output


def compare(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    baseline_sha: str,
    head_sha: str,
    base_paths: set[str],
) -> dict[str, Any]:
    before = Counter(fingerprint(item) for item in baseline)
    after = Counter(fingerprint(item) for item in candidate)
    added = after - before
    resolved = before - after
    by_file: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for (path, code, _message, _source), count in added.items():
        by_file[path] += count
        by_rule[code] += count
    examples = []
    for key, count in sorted(added.items()):
        path, code, message, _source = key
        rows = sorted(x["row"] for x in candidate if fingerprint(x) == key)
        examples.append(
            {
                "path": path,
                "code": code,
                "message": message,
                "first_row": rows[0],
                "count": count,
                "new_file": path not in base_paths,
            }
        )
        if len(examples) == 40:
            break
    return {
        "schema": "q12_full_repo_ruff_baseline_delta_v1",
        "status": "DIAGNOSTIC_ONLY_NOT_RELEASE_APPROVAL",
        "baseline_sha": baseline_sha,
        "candidate_sha": head_sha,
        "baseline_violations": len(baseline),
        "candidate_violations": len(candidate),
        "introduced_violations": sum(added.values()),
        "resolved_violations": sum(resolved.values()),
        "introduced_on_new_files": sum(
            count for key, count in added.items() if key[0] not in base_paths
        ),
        "introduced_on_existing_files": sum(
            count for key, count in added.items() if key[0] in base_paths
        ),
        "baseline_btc_eth_violations": sum(
            x["path"].startswith("btc_eth_terminal/") for x in baseline
        ),
        "candidate_btc_eth_violations": sum(
            x["path"].startswith("btc_eth_terminal/") for x in candidate
        ),
        "introduced_by_rule": dict(sorted(by_rule.items())),
        "introduced_by_file": dict(sorted(by_file.items())),
        "introduced_examples": examples,
        "delta_clean": not added,
        "full_validation_passed": False,
        "production_acceptance": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    base_root = args.base_root.resolve(strict=True)
    candidate_root = args.candidate_root.resolve(strict=True)
    baseline = collect_ruff(base_root)
    candidate = collect_ruff(candidate_root)
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=base_root,
        capture_output=True,
        check=True,
    ).stdout
    base_paths = {x.decode("utf-8") for x in tracked.split(bytes([0])) if x}
    result = compare(
        baseline,
        candidate,
        baseline_sha=args.base_sha,
        head_sha=args.head_sha,
        base_paths=base_paths,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        "Q12_RUFF_DIAGNOSTIC "
        + json.dumps(
            {
                key: result[key]
                for key in (
                    "baseline_violations",
                    "candidate_violations",
                    "introduced_violations",
                    "resolved_violations",
                    "introduced_on_new_files",
                    "introduced_on_existing_files",
                    "delta_clean",
                    "full_validation_passed",
                )
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
