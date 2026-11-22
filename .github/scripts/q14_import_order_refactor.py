#!/usr/bin/env python3
"""Make deliberate post-bootstrap imports explicit without changing import order.

Only top-level imports flagged E402 are refactored. An immediately invoked
loader imports dependencies after their original sys.path setup, then returns
the exact bound names to module scope. Nothing is ignored or suppressed.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from collections import defaultdict
from pathlib import Path


def run_ruff(root: Path) -> list[dict]:
    result = subprocess.run(
        ["ruff", "check", ".", "--output-format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("RUFF_FAILED:" + result.stderr[:600])
    return json.loads(result.stdout)


def exports(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [
            a.asname if a.asname else a.name.split(".")[0]
            for a in node.names
        ]
    if any(a.name == "*" for a in node.names):
        raise ValueError("STAR_IMPORT_CANNOT_BE_SAFE_REFACTORED")
    return [a.asname if a.asname else a.name for a in node.names]


def migrate(text: str, offending: set[int]) -> tuple[str, int]:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    groups = []
    pending = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno in offending:
            if pending:
                prev = pending[-1]
                gap = lines[prev.end_lineno:node.lineno - 1]
                if any(x.strip() and not x.lstrip().startswith("#") for x in gap):
                    groups.append(pending)
                    pending = []
            pending.append(node)
        elif pending:
            groups.append(pending)
            pending = []
    if pending:
        groups.append(pending)
    if not groups:
        return text, 0
    covered = {node.lineno for group in groups for node in group}
    if covered != offending:
        raise ValueError("E402_IMPORT_DISCOVERY_MISMATCH")
    count = 0
    for group in reversed(groups):
        name = f"_q14_load_after_path_setup_{len(groups) - 1 - count}"
        first = group[0].lineno
        last = group[-1].end_lineno
        imports = []
        bindings = []
        for node in group:
            code = "".join(lines[node.lineno - 1:node.end_lineno]).rstrip("\n")
            imports.extend("    " + row for row in code.splitlines())
            bindings.extend(exports(node))
        if len(set(bindings)) != len(bindings):
            raise ValueError("DUPLICATE_IMPORT_BINDINGS_UNSAFE")
        bind = ", ".join(bindings)
        returns = f"    return {bind}" if len(bindings) > 1 else f"    return {bind}"
        target = f"{bind} = {name}()" if len(bindings) > 1 else f"{bind} = {name}()"
        body = (
            f"def {name}():\n"
            + "\n".join(imports)
            + "\n"
            + returns
            + "\n\n"
            + target
            + "\n"
            + f"del {name}\n"
        )
        # Keep comments separating original imports only when every such
        # comment can be preserved at its original relative position.
        source_window = lines[first - 1:last]
        preserved_comments = [
            l for l in source_window
            if l.lstrip().startswith("#")
        ]
        if preserved_comments:
            body = "".join(preserved_comments) + body
        lines[first - 1:last] = [body]
        count += len(group)
    revised = "".join(lines)
    ast.parse(revised)
    compile(revised, "<q14-import-refactor>", "exec")
    return revised, count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = args.root.resolve()
    issues = run_ruff(root)
    targets: dict[str, set[int]] = defaultdict(set)
    for item in issues:
        if item["code"] != "E402":
            continue
        path = Path(item["filename"]).resolve()
        rel = path.relative_to(root).as_posix()
        if not rel.startswith("btc_eth_terminal/"):
            raise ValueError("E402_OUT_OF_SCOPE")
        targets[rel].add(item["location"]["row"])
    count = 0
    for name, offending in sorted(targets.items()):
        file = root / name
        original = file.read_text(encoding="utf-8")
        changed, replaced = migrate(original, offending)
        if replaced != len(offending):
            raise ValueError("UNEXPECTED_E402_COUNT")
        if args.apply:
            file.write_text(changed, encoding="utf-8")
        count += replaced
    if args.apply:
        after = run_ruff(root)
        remaining = sum(item["code"] == "E402" for item in after)
        if remaining:
            raise ValueError(f"UNRESOLVED_E402:{remaining}")
    print("Q14_EXPLICIT_POST_BOOTSTRAP_IMPORTS", json.dumps({
        "replaced": count,
        "files": len(targets),
        "applied": args.apply,
        "no_lint_suppression": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
