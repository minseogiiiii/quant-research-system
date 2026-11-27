"""First-pass mechanical remediation; never edit pinned or vendor files."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCOPES = (
    "btc_eth_terminal/backend",
    "btc_eth_terminal/scripts",
    "btc_eth_terminal/tests",
)


def run(*args):
    return subprocess.run(args, capture_output=True, check=False)


def lint(*paths):
    p = run("ruff", "check", "--output-format", "json", *paths)
    if p.returncode not in (0, 1):
        raise RuntimeError(p.stderr.decode())
    return json.loads(p.stdout)


def counts(rows):
    return Counter(row["code"] for row in rows)


def semantic(raw):
    return ast.dump(ast.parse(raw, type_comments=True), include_attributes=False)


def pinned(path, raw):
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    digest = hashlib.sha256(raw).hexdigest()
    for key in (blob, digest):
        p = run("git", "grep", "-l", "-F", key, "--", ".", f":(exclude){path}")
        if p.returncode == 0:
            return True
        if p.returncode != 1:
            raise RuntimeError("git grep failed")
    return False


def main():
    output = Path(sys.argv[1])
    before = lint(*SCOPES)
    files = sorted({Path(row["filename"]).relative_to(Path.cwd()) for row in before})
    result = {"before": dict(counts(before)), "changed": [], "pinned": [], "rejected": []}
    for path in files:
        if path.suffix != ".py" or not any(path.is_relative_to(s) for s in SCOPES):
            raise RuntimeError("Unexpected lint path")
        raw = path.read_bytes()
        if pinned(path, raw):
            result["pinned"].append(str(path))
            continue
        old = counts(lint(str(path)))
        try:
            formatted = run("ruff", "format", str(path))
            if formatted.returncode:
                raise ValueError("format failed")
            new = path.read_bytes()
            if new == raw:
                continue
            if semantic(raw) != semantic(new):
                raise ValueError("AST changed")
            new_counts = counts(lint(str(path)))
            if new_counts - old:
                raise ValueError("new lint diagnostic")
            if sum(new_counts.values()) >= sum(old.values()):
                raise ValueError("no lint improvement")
            result["changed"].append(str(path))
        except Exception as exc:
            path.write_bytes(raw)
            result["rejected"].append({"file": str(path), "reason": str(exc)})
    after = lint(*SCOPES)
    result["after"] = dict(counts(after))
    result["before_total"] = len(before)
    result["after_total"] = len(after)
    result["remaining"] = [
        {"file": str(Path(r["filename"]).relative_to(Path.cwd())),
         "code": r["code"], "line": r["location"]["row"], "message": r["message"]}
        for r in after
    ]
    if counts(after) - counts(before):
        raise RuntimeError("aggregate new lint diagnostic")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print("U1_FORMAT_RESULT", len(before), len(after), len(result["changed"]))


if __name__ == "__main__":
    main()
