"""Verify an isolated, genuine GitHub-hosted Coinbase public-source smoke cohort.

This audit authenticates the *bytes of the retrieved GitHub Actions artifact*
against a pinned artifact digest and checks independent software replay. It
cannot authenticate the exchange's clock, fill allocation, private execution,
24-hour continuity, or original historical Feature/Label availability.
"""

from __future__ import annotations

import argparse
import json
import stat
from datetime import datetime
from hashlib import sha1, sha256
from pathlib import Path
from zipfile import ZipFile

from audit_soak import audit as replay_a
from independent_soak_replay import replay as replay_b
from v25_capture_admission import load_json

ARTIFACT_SHA = "8720e127b506f9a38d04113f012c56fc4c26115b67ad941fa2734287bd87d55f"
MANIFEST_SHA = "a68a147322575274f455c02fcf5888df1d903cc7a4b21d6e5f8b4e71abc4e517"
COLLECTOR_BLOB_SHA = "21846a08a184f53fcb4c8702d1937269a8f552e4"
RUN_COMMIT = "fdc8bcf16528454f79d2165df5b18617f9b7831b"
RUN_ID = 43696932946
ARTIFACT_ID = 14155116586
SOURCE_SUBDIR = "coinbase_btc_eth_soak_n9dQoG"
MAX_MEMBERS = 32
MAX_UNCOMPRESSED_BYTES = 100_000_000


class V27AuditError(ValueError):
    pass


def digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as stream:
        for buf in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def check_git_blob(path: Path) -> str:
    data = path.read_bytes()
    return sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def verify_artifact_bundle(evidence: Path) -> dict:
    """Verify each extracted source byte against pinned original ZIP bytes."""
    artifact = evidence / "github_workflow_artifact.zip"
    if artifact.is_symlink() or not artifact.is_file():
        raise V27AuditError("MISSING_OR_SYMLINKED_GITHUB_ARTIFACT")
    if digest(artifact) != ARTIFACT_SHA:
        raise V27AuditError("GITHUB_ARTIFACT_SHA_MISMATCH")
    expected = {
        f"{SOURCE_SUBDIR}/manifest.json",
        f"{SOURCE_SUBDIR}/chunks/chunk_00000_conn_000.jsonl",
        f"{SOURCE_SUBDIR}/chunks/chunk_00001_conn_000.jsonl",
        "v27_workflow_byte_check.json",
    }
    actual = set()
    with ZipFile(artifact) as z:
        members = z.infolist()
        if (
            len(members) > MAX_MEMBERS
            or sum(x.file_size for x in members) > MAX_UNCOMPRESSED_BYTES
        ):
            raise V27AuditError("EXCESSIVE_GITHUB_ARTIFACT_MEMBERS_OR_SIZE")
        for member in members:
            name = member.filename
            if (
                member.is_dir()
                or not name
                or name.startswith("/")
                or "\\" in name
                or any(seg in ("", ".", "..") for seg in name.split("/"))
                or stat.S_ISLNK(member.external_attr >> 16)
                or name in actual
            ):
                raise V27AuditError("UNSAFE_OR_DUPLICATE_ARTIFACT_MEMBER")
            actual.add(name)
            target = evidence / (
                "source/" + name if name != "v27_workflow_byte_check.json" else name
            )
            if (
                target.is_symlink()
                or not target.is_file()
                or not target.resolve().is_relative_to(evidence.resolve())
            ):
                raise V27AuditError("EXTRACTED_FILE_MISSING_OR_UNSAFE")
            h = sha256()
            with z.open(member) as reader:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != digest(target):
                raise V27AuditError("EXTRACTED_SOURCE_DIFFERS_FROM_GITHUB_ARTIFACT")
    if actual != expected:
        raise V27AuditError("GITHUB_ARTIFACT_FILE_SET_MISMATCH")
    return {
        "artifact_sha256": ARTIFACT_SHA,
        "artifact_members": len(actual),
        "extracted_bytes_identical_to_github_artifact": True,
    }


def verify_manifest_contract(manifest: dict, workflow_check: dict) -> None:
    if not isinstance(manifest, dict) or not isinstance(workflow_check, dict):
        raise V27AuditError("INVALID_CAPTURE_OR_WORKFLOW_REPORT")
    if (
        manifest.get("source") != "wss://advanced-trade-ws.coinbase.com"
        or manifest.get("products") != ["BTC-USD", "ETH-USD"]
        or manifest.get("subscribed_channels")
        != ["level2", "market_trades", "heartbeats"]
        or manifest.get("required_duration_seconds") != 60
        or manifest.get("producer_release") != "v13_rolling_bounded"
        or manifest.get("status") != "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        or manifest.get("ready_for_research") is not True
        or manifest.get("synthetic_fixture", False) is not False
        or manifest.get("orders_enabled") is not False
        or manifest.get("faults") != []
        or manifest.get("total_reconnects") != 0
        or manifest.get("messages") != 2706
        or len(manifest.get("chunks", [])) != 2
    ):
        raise V27AuditError("UNEXPECTED_OR_QUARANTINED_CAPTURE_CONTRACT")
    conn = manifest.get("connections")
    if (
        not isinstance(conn, list)
        or len(conn) != 1
        or conn[0].get("integrity_fault") is not False
        or conn[0].get("heartbeat_count") != 60
        or conn[0].get("last_sequence") != 2705
        or not all(
            conn[0].get("snapshots", {}).get(p) is True
            and conn[0].get("updates", {}).get(p, 0) > 0
            for p in ("BTC-USD", "ETH-USD")
        )
    ):
        raise V27AuditError("CONNECTION_OR_SNAPSHOT_FAULT")
    if (
        workflow_check.get("status")
        != "BYTE_CONSISTENT_ONE_CONNECTION_NOT_EXCHANGE_ATTESTED"
        or workflow_check.get("manifest_sha256") != MANIFEST_SHA
        or workflow_check.get("message_count") != 2706
        or workflow_check.get("chunk_count") != 2
        or workflow_check.get("source_connections") != 1
        or workflow_check.get("independent_exchange_truth") is not False
        or workflow_check.get("order_count") != 0
    ):
        raise V27AuditError("WORKFLOW_CHECK_IS_NOT_THE_PINNED_RESULT")
    try:
        start = datetime.fromisoformat(
            manifest["sampled_from_utc"].replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(manifest["finished_at_utc"].replace("Z", "+00:00"))
        elapsed = (end - start).total_seconds()
    except (ValueError, TypeError, KeyError) as e:
        raise V27AuditError("INVALID_CAPTURE_TIME") from e
    if not 58 <= elapsed <= 120:
        raise V27AuditError("WRONG_CAPTURE_DURATION")


def verify(evidence_root: Path, code_root: Path | None = None) -> dict:
    evidence_root = Path(evidence_root)
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise V27AuditError("MISSING_OR_UNSAFE_EVIDENCE_ROOT")
    code_root = (
        Path(code_root)
        if code_root is not None
        else Path(__file__).resolve().parents[1]
    )
    collector_path = code_root / "scripts/btc_eth_rolling_v14.mjs"
    if (
        collector_path.is_symlink()
        or check_git_blob(collector_path) != COLLECTOR_BLOB_SHA
    ):
        raise V27AuditError("GITHUB_CAPTURE_SCRIPT_NOT_IDENTICAL_TO_FROZEN_BASELINE")
    bundle = verify_artifact_bundle(evidence_root)
    source = evidence_root / "source" / SOURCE_SUBDIR
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or digest(manifest_path) != MANIFEST_SHA:
        raise V27AuditError("SOURCE_MANIFEST_SHA_MISMATCH")
    manifest = load_json(manifest_path)
    workflow_check = load_json(evidence_root / "v27_workflow_byte_check.json")
    verify_manifest_contract(manifest, workflow_check)
    first = replay_a(source)
    second = replay_b(source)
    if (
        first.get("status") != "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
        or second.get("status") != "INDEPENDENT_SOURCE_REPLAY_ONLY"
        or first.get("message_count") != second.get("messages")
        or first.get("message_count") != manifest["messages"]
        or first.get("chunk_count") != second.get("chunks")
        or first.get("connection_count") != second.get("connections")
    ):
        raise V27AuditError("INDEPENDENT_REPLAY_SOURCE_COUNTS_NOT_IDENTICAL")
    books = {}
    for symbol in ("BTC-USD", "ETH-USD"):
        one = first["books_final"][symbol]
        two = second["books_final"][symbol]
        if one != two:
            raise V27AuditError("INDEPENDENT_ORDER_BOOK_DISAGREEMENT:" + symbol)
        books[symbol] = {
            "final_book_sha256": one["sha256"],
            "best_bid": one["best_bid"],
            "best_ask": one["best_ask"],
        }
    return {
        "schema": "v27_github_public_capture_audit_v1",
        "status": "REAL_PUBLIC_60S_SOURCE_ARCHIVED_INTERNALLY_DOUBLE_REPLAYED",
        "source_lineage": "v14_collector_with_v13_rolling_bounded_manifest_marker",
        "source_archive": SOURCE_SUBDIR,
        "github_branch": "evidence/btc-eth-v27-real-smoke-20261115",
        "github_workflow_run_id": RUN_ID,
        "github_workflow_commit_sha": RUN_COMMIT,
        "github_artifact_id": ARTIFACT_ID,
        "github_workflow_url": "https://github.com/minseogiiiii/quant-research-system/actions/runs/"
        + str(RUN_ID),
        "collector_git_blob_sha1": COLLECTOR_BLOB_SHA,
        "source_manifest_sha256": MANIFEST_SHA,
        **bundle,
        "source_claimed_from": manifest["source"],
        "capture_start_utc_host_reported": manifest["sampled_from_utc"],
        "capture_end_utc_host_reported": manifest["finished_at_utc"],
        "messages": first["message_count"],
        "chunks": first["chunk_count"],
        "connections": first["connection_count"],
        "heartbeats": manifest["connections"][0]["heartbeat_count"],
        "reconnections": 0,
        "source_reported_faults": 0,
        "verified_independent_software_replay_of_same_source": True,
        "book_results": books,
        "v26_host_monitored_clock_evidence_available": False,
        "independent_exchange_truth_attested": False,
        "original_feature_label_bytes_recovered": False,
        "historical_feature_availability_attested": False,
        "research_blind_oos_eligible": False,
        "one_continuous_24h_verified": False,
        "private_order_execution_or_queue_attested": False,
        "real_orders_sent": 0,
        "confirmed_executable_alpha": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or args.output.is_symlink():
        p.error("Refusing to overwrite existing output")
    try:
        result = verify(args.evidence)
    except (ValueError, KeyError, OSError, TypeError, OverflowError) as exc:
        result = {
            "schema": "v27_github_public_capture_audit_v1",
            "status": "REJECTED_OR_MISSING_SOURCE_EVIDENCE",
            "reason": str(exc)[:500],
            "confirmed_executable_alpha": False,
            "real_orders_sent": 0,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(result["status"])
    if result["status"] == "REJECTED_OR_MISSING_SOURCE_EVIDENCE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
