#!/usr/bin/env python3
"""Split long Python string literals without changing any parsed AST.

Only Ruff E501 sites are eligible. Each rewrite is accepted if and only if
ast.dump(..., include_attributes=False) is unchanged for the entire file.
No import moves, lint suppressions or product arithmetic changes.
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
import tokenize
from collections import Counter, defaultdict
from pathlib import Path


def lint() -> list[dict]:
    run = subprocess.run(
        ["ruff", "check", ".", "--output-format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode not in (0, 1):
        raise RuntimeError("Q14_RUFF_SCAN_FAILED")
    return json.loads(run.stdout)


def attempt_literal_split(source: str, row: int) -> str | None:
    lines = source.splitlines(keepends=True)
    if row < 1 or row > len(lines) or len(lines[row - 1].rstrip("\n")) <= 88:
        return None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    options = []
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        if token.start[0] != row or token.end[0] != row:
            continue
        try:
            value = ast.literal_eval(token.string)
        except (ValueError, SyntaxError, TypeError, UnicodeError):
            continue
        if isinstance(value, (str, bytes)) and len(value) >= 38:
            options.append((len(token.string), token, value))
    options.sort(reverse=True, key=lambda item: item[0])
    original_tree = ast.dump(ast.parse(source), include_attributes=False)
    line = lines[row - 1]
    leading = line[:len(line) - len(line.lstrip())]
    for _, token, value in options:
        pieces = [value[i:i + 28] for i in range(0, len(value), 28)]
        replacement = (
            "(\n"
            + "".join(leading + "    " + repr(piece) + "\n" for piece in pieces)
            + leading + ")"
        )
        if any(len(x) > 88 for x in replacement.splitlines()):
            continue
        updated_line = (
            line[:token.start[1]] + replacement + line[token.end[1]:]
        )
        candidate = (
            "".join(lines[:row - 1]) + updated_line + "".join(lines[row:])
        )
        try:
            changed_tree = ast.dump(ast.parse(candidate), include_attributes=False)
            if original_tree == changed_tree:
                return candidate
        except SyntaxError:
            continue
    return None


def main() -> None:
    root = Path.cwd().resolve()
    original = lint()
    issues: dict[Path, set[int]] = defaultdict(set)
    for item in original:
        if item["code"] != "E501":
            continue
        file = Path(item["filename"]).resolve(strict=True)
        rel = file.relative_to(root)
        if len(rel.parts) < 3 or rel.parts[0] != "btc_eth_terminal":
            continue
        if file.suffix == ".py":
            issues[file].add(item["location"]["row"])
    fixes = 0
    touched = 0
    for file, rows in sorted(issues.items()):
        source = file.read_text(encoding="utf-8")
        baseline = ast.dump(ast.parse(source), include_attributes=False)
        result = source
        accepted = 0
        for row in sorted(rows, reverse=True):
            candidate = attempt_literal_split(result, row)
            if candidate is not None:
                result = candidate
                accepted += 1
        if result != source:
            if ast.dump(ast.parse(result), include_attributes=False) != baseline:
                raise RuntimeError("Q14_LITERAL_SPLIT_CHANGED_AST")
            file.write_text(result, encoding="utf-8")
            fixes += accepted
            touched += 1
    updated = lint()
    by_rule = Counter(item["code"] for item in updated)
    print(
        "Q14_AST_PRESERVED_LITERAL_REFACTOR",
        json.dumps(
            {
                "attempted_issues": sum(len(v) for v in issues.values()),
                "accepted_string_sites": fixes,
                "changed_files": touched,
                "before": len(original),
                "after": len(updated),
                "remaining_by_rule": dict(sorted(by_rule.items())),
                "full_AST_identical_for_all_modified_files": True,
                "lint_suppression_used": False,
            },
            sort_keys=True,
        ),
    )
    if not fixes or len(updated) >= len(original):
        raise RuntimeError("Q14_NO_VERIFIED_LINT_REDUCTION")


if __name__ == "__main__":
    main()
