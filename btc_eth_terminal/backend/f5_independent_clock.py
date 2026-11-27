"""Independent raw receiver UTC/sequence check, no float epoch conversion."""

import hashlib
import json
import re
from datetime import date

STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z")
EPOCH = date(1970, 1, 1)


def milliseconds(text):
    m = STAMP.fullmatch(text)
    if m is None:
        raise ValueError("F5_CLOCK_FORMAT")
    y, mo, d, h, mi, s = map(int, m.groups()[:6])
    if h > 23 or mi > 59 or s > 59:
        raise ValueError("F5_CLOCK_DOMAIN")
    return (
        (date(y, mo, d) - EPOCH).days * 86400 + h * 3600 + mi * 60 + s
    ) * 1000 + int((m.group(7) or "").ljust(3, "0")[:3])


def unique(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("F5_CLOCK_DUPLICATE_KEY")
        d[k] = v
    return d


def nonfinite(_):
    raise ValueError("F5_CLOCK_NONFINITE")


def read_independent(root, manifest):
    count = 0
    last = None
    for chunk in manifest["chunks"]:
        name = chunk["file"]
        if not re.fullmatch(r"chunks/chunk_\d{5}_conn_\d{3}\.jsonl", name):
            raise ValueError("F5_CLOCK_PATH")
        path = root / name
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("F5_CLOCK_SYMLINK")
        with path.open("rb") as f:
            data = f.read(32000001)
        if (
            len(data) != chunk["archived_line_bytes"]
            or hashlib.sha256(data).hexdigest() != chunk["archive_sha256"]
        ):
            raise ValueError("F5_CLOCK_CHUNK")
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise ValueError("F5_CLOCK_TRUNCATED")
            row = json.loads(line, object_pairs_hook=unique, parse_constant=nonfinite)
            raw = row["raw_text"]
            wire = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
            if (
                row["connection_id"] != 0
                or hashlib.sha256(raw.encode()).hexdigest() != row["sha256"]
                or type(wire["sequence_num"]) is not int
                or wire["sequence_num"] != count
            ):
                raise ValueError("F5_CLOCK_SEQUENCE")
            t = milliseconds(row["received_utc"])
            if last is not None and t < last:
                raise ValueError("F5_CLOCK_REGRESSION")
            last = t
            yield count, t, wire
            count += 1
    if count != manifest["messages"]:
        raise ValueError("F5_CLOCK_COUNT")
