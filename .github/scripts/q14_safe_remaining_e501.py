#!/usr/bin/env python3
"""Reflow E501 comments, f-strings and triple literals with identical AST."""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import textwrap
import tokenize
from collections import Counter, defaultdict
from pathlib import Path


def same_ast(left: str, right: str) -> bool:
    try:
        a = ast.dump(ast.parse(left), include_attributes=False)
        b = ast.dump(ast.parse(right), include_attributes=False)
        return a == b
    except (SyntaxError, ValueError):
        return False


def fix_line(source: str, row: int) -> tuple[str | None, str | None]:
    lines = source.splitlines(keepends=True)
    if not 1 <= row <= len(lines):
        return None, None
    line = lines[row - 1]
    if len(line.rstrip("\n")) <= 88:
        return None, None
    leading = line[:len(line) - len(line.lstrip())]
    stripped = line.lstrip()

    if stripped.startswith("#") and not re.match(
        r"#\s*(?:noqa|type:|ruff|fmt|pylint|mypy|coding|!|\s*$)",
        stripped,
        re.I,
    ):
        value = stripped.lstrip("# ").rstrip("\n")
        if len(value) > 35 and len(leading) < 44 and "https://" not in value:
            pieces = textwrap.wrap(
                value,
                width=86 - len(leading) - 2,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if len(pieces) > 1:
                proposal = "".join(leading + "# " + part + "\n" for part in pieces)
                updated = "".join(lines[:row - 1]) + proposal + "".join(lines[row:])
                if same_ast(source, updated):
                    return updated, "comment"

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        tokens = []
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        if not token.start[0] <= row <= token.end[0]:
            continue
        if not any(quote in token.string for quote in ('"""', "'''")):
            continue
        lower = max(10, token.start[1] + 5 if token.start[0] == row else 10)
        for index in range(min(72, len(line) - 15), lower, -1):
            if line[index - 1] not in " ,;|:":
                continue
            candidate_line = line[:index] + "\\\n" + line[index:]
            changed = (
                "".join(lines[:row - 1]) + candidate_line + "".join(lines[row:])
            )
            if same_ast(source, changed):
                return changed, "triple_literal"

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, None
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        and node.lineno == row
        and node.end_lineno == row
    ]
    for node in sorted(
        candidates,
        key=lambda n: n.end_col_offset - n.col_offset,
        reverse=True,
    ):
        segment = ast.get_source_segment(source, node)
        if not segment:
            continue
        match = re.match(r"""(?i)^(rf|fr|f)(["'])""", segment)
        if not match or not segment.endswith(match.group(2)):
            continue
        if len(segment) < 42:
            continue
        prefix, quote = match.groups()
        body = segment[len(prefix) + 1:-1]
        boundaries = sorted(
            (i for i in range(14, len(body) - 14) if body[i] == " "),
            key=lambda i: abs(i - len(body) // 2),
        )
        for index in boundaries:
            left, right = body[:index], body[index:]
            proposal = (
                "(\n"
                + leading + "    " + prefix + quote + left + quote + "\n"
                + leading + "    " + prefix + quote + right + quote + "\n"
                + leading + ")"
            )
            trial = line[:node.col_offset] + proposal + line[node.end_col_offset:]
            changed = "".join(lines[:row - 1]) + trial + "".join(lines[row:])
            if same_ast(source, changed):
                return changed, "fstring"
    return None, None


def scan() -> list[dict]:
    process = subprocess.run(
        ["ruff", "check", ".", "--output-format", "json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode not in (0, 1):
        raise RuntimeError("RUFF_FAILED")
    return json.loads(process.stdout)


def main() -> None:
    root = Path.cwd().resolve()
    before = scan()
    sites: dict[Path, set[int]] = defaultdict(set)
    for issue in before:
        if issue["code"] != "E501":
            continue
        path = Path(issue["filename"]).resolve(strict=True)
        rel = path.relative_to(root)
        if rel.parts[0] == "btc_eth_terminal" and path.suffix == ".py":
            sites[path].add(issue["location"]["row"])
    fixed = Counter()
    touched = 0
    for file, rows in sorted(sites.items()):
        original = file.read_text(encoding="utf-8")
        changed = original
        for row in sorted(rows, reverse=True):
            candidate, reason = fix_line(changed, row)
            if candidate is not None:
                changed = candidate
                fixed[reason] += 1
        if changed != original:
            if not same_ast(original, changed):
                raise RuntimeError("Q14_NONIDENTICAL_AST_BLOCKED")
            file.write_text(changed, encoding="utf-8")
            touched += 1
    after = scan()
    report = {
        "before": len(before),
        "after": len(after),
        "resolved": len(before) - len(after),
        "changed_files": touched,
        "fixed_by_category": dict(fixed),
        "all_changed_AST_identical": True,
        "lint_suppression": False,
    }
    print("Q14_SAFE_COMMENT_DOC_FSTRING_REFLOW", json.dumps(report, sort_keys=True))
    if len(after) >= len(before) or not touched:
        raise RuntimeError("Q14_NO_SAFE_IMPROVEMENT")


if __name__ == "__main__":
    main()
