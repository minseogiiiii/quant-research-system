"""Read-only crash evidence inspection; NEVER recover or admit a trading feed.

The source manifest is an untrusted journal, not a proof of durable writes.
This tool reports completed, unlisted, partially written and corrupt source rows
for forensic investigation only. Partial source is NEVER automatically promoted
into an as-of or live trade-research feed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from audit_soak import SoakAuditError, _file, _json

FILE = re.compile(r"chunk_\d{5}_conn_\d{3}\.jsonl(?:\.partial)?\Z")
MAX_ROW_BYTES = 18_500_000
MAX_FILE_BYTES = 18_500_000
MAX_TOTAL_BYTES = 340_000_000


def inspect(directory: Path) -> dict:
    if directory.is_symlink():
        raise SoakAuditError("Symlink evidence root")
    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise SoakAuditError("Evidence root is not a directory")
    _file(root / "manifest.json", root, 500_000)
    manifest = _json((root / "manifest.json").read_bytes())
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "btc_eth_raw_soak_v10"
        or manifest.get("source") != "wss://advanced-trade-ws.coinbase.com"
        or manifest.get("products") != ["BTC-USD", "ETH-USD"]
        or manifest.get("orders_enabled") is not False
        or manifest.get("strategy_execution_enabled") is not False
    ):
        raise SoakAuditError("Incompatible or unsafe source manifest")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or len(chunks) > 10000:
        raise SoakAuditError("Invalid committed chunk ledger")
    expected = {}
    for i, item in enumerate(chunks):
        if not isinstance(item, dict) or type(item.get("connection_id")) is not int:
            raise SoakAuditError("Invalid manifest chunk entry")
        expected_name = f"chunk_{i:05d}_conn_{item['connection_id']:03d}.jsonl"
        if item.get("file") != "chunks/" + expected_name or expected_name in expected:
            raise SoakAuditError("Noncanonical committed chunk entry")
        expected[expected_name] = item
    chunk_dir = root / "chunks"
    if chunk_dir.is_symlink() or not chunk_dir.is_dir():
        raise SoakAuditError("Missing chunks directory")
    observed = sorted(chunk_dir.iterdir())
    if len(observed) > 10001:
        raise SoakAuditError("Too many evidence files")
    partial = []
    unexpected = []
    committed = []
    accumulated_bytes = 0
    all_rows = 0
    for file in observed:
        if not FILE.fullmatch(file.name):
            unexpected.append(file.name)
            continue
        size = _file(file, root, MAX_FILE_BYTES)
        accumulated_bytes += size
        if accumulated_bytes > MAX_TOTAL_BYTES:
            raise SoakAuditError("Archive exceeds forensic limit")
        record_count = 0
        incomplete_tail = False
        digest = hashlib.sha256()
        first_seq = last_seq = None
        with file.open("rb") as handle:
            for row in handle:
                if len(row) > MAX_ROW_BYTES:
                    raise SoakAuditError("Oversized archived source row")
                digest.update(row)
                if not row.endswith(b"\n"):
                    incomplete_tail = True
                    break
                doc = _json(row)
                if not isinstance(doc, dict) or not isinstance(
                    doc.get("raw_text"), str
                ):
                    raise SoakAuditError("Invalid raw source record")
                raw = doc["raw_text"].encode("utf-8")
                if hashlib.sha256(raw).hexdigest() != doc.get("sha256"):
                    raise SoakAuditError("Altered embedded source SHA")
                event = _json(raw)
                if (
                    not isinstance(event, dict)
                    or type(event.get("sequence_num")) is not int
                ):
                    raise SoakAuditError("Invalid source sequence record")
                seq = event["sequence_num"]
                if seq < 0 or (last_seq is not None and seq != last_seq + 1):
                    raise SoakAuditError("Gap/replay inside source file")
                if (
                    file.name.endswith(".partial")
                    and type(doc.get("connection_id")) is not int
                ):
                    raise SoakAuditError("Unidentified partial source connection")
                if first_seq is None:
                    first_seq = seq
                last_seq = seq
                record_count += 1
        all_rows += record_count
        entry = {
            "file": file.name,
            "bytes": size,
            "complete_lines": record_count,
            "sha256": digest.hexdigest(),
            "first_sequence": first_seq,
            "last_sequence": last_seq,
            "truncated_tail": incomplete_tail,
        }
        if file.name in expected:
            declared = expected[file.name]
            if (
                incomplete_tail
                or digest.hexdigest() != declared.get("archive_sha256")
                or record_count != declared.get("messages")
                or size != declared.get("archived_line_bytes")
            ):
                raise SoakAuditError("Committed archived chunk changed or truncated")
            committed.append(entry)
        elif file.name.endswith(".partial"):
            partial.append(entry)
        else:
            unexpected.append(file.name)
    if {x["file"] for x in committed} != set(expected):
        raise SoakAuditError("Missing committed source chunk")
    return {
        "status": "CRASH_FORENSICS_ONLY_NOT_ADMITTED",
        "manifest_status": manifest.get("status"),
        "committed_chunks": len(committed),
        "partial_chunks": partial,
        "unlisted_files": unexpected,
        "manifest_tmp_exists": (root / "manifest.json.tmp").exists(),
        "complete_source_rows_seen": all_rows,
        "archived_bytes_seen": accumulated_bytes,
        "any_partial_tail_detected": any(x["truncated_tail"] for x in partial),
        "eligible_for_point_in_time_feature_research": False,
        "independently_authenticated_market_truth": False,
        "live_orders_enabled": False,
        "requires_new_snapshot_after_restart": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = inspect(args.archive)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {
            "status": "FORENSIC_INSPECTION_REJECTED",
            "error": str(exc)[:500],
            "eligible_for_point_in_time_feature_research": False,
            "live_orders_enabled": False,
        }
    if args.report:
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "CRASH_FORENSICS_ONLY_NOT_ADMITTED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
