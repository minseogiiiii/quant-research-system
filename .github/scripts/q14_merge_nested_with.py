#!/usr/bin/env python3
"""Collapse nested Python with statements with a normalized-AST proof.

Scoped to Ruff SIM117 sites only; both original and candidate must compile to
equivalent ordered context-manager acquisition and body statements. Comments,
literals and all other AST nodes must remain intact.
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
import tokenize
from collections import Counter, defaultdict
from pathlib import Path


def comment_multiset(source: str) -> Counter[str]:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return Counter(t.string for t in tokens if t.type == tokenize.COMMENT)


class NormalizeNestedWith(ast.NodeTransformer):
    def visit_With(self, node: ast.With) -> ast.With:
        self.generic_visit(node)
        while len(node.body) == 1 and isinstance(node.body[0], ast.With):
            child = node.body[0]
            if node.type_comment or child.type_comment:
                break
            node.items = [*node.items, *child.items]
            node.body = child.body
        return node


def equivalent_ast(source: str) -> str:
    tree = ast.parse(source)
    return ast.dump(NormalizeNestedWith().visit(tree), include_attributes=False)


def line_indentation(source: str) -> int:
    return len(source) - len(source.lstrip(" "))


def with_item(item: ast.withitem) -> str:
    context = ast.unparse(item.context_expr)
    if item.optional_vars is not None:
        context += " as " + ast.unparse(item.optional_vars)
    return context


def rewrite_one(source: str, row: int) -> str | None:
    lines = source.splitlines(keepends=True)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    eligible = [
        x
        for x in ast.walk(tree)
        if isinstance(x, ast.With)
        and x.lineno == row
        and len(x.body) == 1
        and isinstance(x.body[0], ast.With)
        and not x.type_comment
        and not x.body[0].type_comment
    ]
    if len(eligible) != 1:
        return None
    outer = eligible[0]
    inner = outer.body[0]
    outer_indent = line_indentation(lines[outer.lineno - 1])
    inner_indent = line_indentation(lines[inner.lineno - 1])
    difference = inner_indent - outer_indent
    if difference <= 0 or difference > 8:
        return None
    first_child_row = inner.body[0].lineno
    if first_child_row <= inner.lineno:
        return None

    # Restrict to pure header text between the outer and inner statements.
    # Any comments there would require preserving their original ownership.
    for source_line in lines[outer.lineno - 1:inner.lineno - 1]:
        if "#" in source_line:
            return None
    for source_line in lines[inner.lineno - 1:first_child_row - 1]:
        if "#" in source_line:
            return None

    ws = " " * outer_indent
    header = [ws + "with (\n"]
    for item in [*outer.items, *inner.items]:
        expression = with_item(item)
        header.append(ws + "    " + expression + ",\n")
    header.append(ws + "):\n")

    original_body = lines[first_child_row - 1:outer.end_lineno]
    shifted = []
    for line in original_body:
        if not line.strip():
            shifted.append(line)
        elif line[:difference] == " " * difference:
            shifted.append(line[difference:])
        else:
            return None

    update = (
        "".join(lines[:outer.lineno - 1])
        + "".join(header)
        + "".join(shifted)
        + "".join(lines[outer.end_lineno:])
    )
    try:
        if equivalent_ast(source) != equivalent_ast(update):
            return None
        if comment_multiset(source) != comment_multiset(update):
            return None
        compile(update, "<q14-merged-with>", "exec")
    except (SyntaxError, IndentationError, tokenize.TokenError, ValueError):
        return None
    return update


def scan_sim117() -> list[dict]:
    proc = subprocess.run(
        ["ruff", "check", ".", "--select", "SIM117", "--output-format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr[:800])
    return json.loads(proc.stdout)


def main() -> None:
    root = Path.cwd().resolve()
    initial = scan_sim117()
    by_file: dict[Path, set[int]] = defaultdict(set)
    for issue in initial:
        path = Path(issue["filename"]).resolve(strict=True)
        rel = path.relative_to(root)
        if not rel.parts or rel.parts[0] != "btc_eth_terminal":
            raise RuntimeError("SIM117_OUT_OF_SCOPE")
        by_file[path].add(issue["location"]["row"])
    saved = 0
    untouched = []
    for path, rows in sorted(by_file.items()):
        before = path.read_text()
        current = before
        for row in sorted(rows, reverse=True):
            next_content = rewrite_one(current, row)
            if next_content is None:
                untouched.append(path.as_posix() + ":" + str(row))
            else:
                current = next_content
                saved += 1
        if current != before:
            if equivalent_ast(current) != equivalent_ast(before):
                raise RuntimeError("Q14_WITH_AST_MISMATCH")
            if comment_multiset(current) != comment_multiset(before):
                raise RuntimeError("Q14_WITH_COMMENT_LOSS")
            path.write_text(current)

    after = scan_sim117()
    if len(after) >= len(initial):
        raise RuntimeError("Q14_NO_CONTEXT_MANAGER_REDUCTION")
    print(
        "Q14_CONTEXT_MANAGER_SEMANTIC_PARITY",
        json.dumps(
            {
                "previous": len(initial),
                "remaining": len(after),
                "merged": saved,
                "not_changed": untouched,
                "normalized_AST_identical": True,
                "comments_preserved": True,
                "no_ignores": True,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
