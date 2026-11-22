"""Pinned, read-only audit for one genuine clean and one fault-injected \
provider capture.

A controlled disconnect is not an unplanned outage, and neither recorded
session proves third-party order book truth, executable fills or profitable alpha.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from audit_soak import audit
from independent_soak_replay import replay


class RecoveryEvidenceError(ValueError):
    pass


PROVENANCE = {
    "clean": {
        "relative_dir": "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy",
        "manifest_sha256": (
            '8f2e0ec88a601a92ab4c7bcd0e04'
            'b20536fbe46bbd8b3c7f67ae3c1d'
            '075c28e5'
        ),
        "messages": 5177,
        "chunks": 28,
        "connections": 1,
        "artifact_sha256": (
            '4211a5e9c30e15d2e64826b59661'
            '2235f42a3b3ced2f8fd2627c8064'
            '19c724a2'
        ),
    },
    "injected": {
        "relative_dir": (
            'backend/data/live_v14_discon'
            'nect_120s/coinbase_btc_eth_s'
            'oak_gEF6uZ'
        ),
        "manifest_sha256": (
            '35e024fc87b317ebcf06c452562e'
            'f809a996bcc350c581356b7394c8'
            '022e5955'
        ),
        "messages": 5164,
        "chunks": 31,
        "connections": 2,
        "artifact_sha256": (
            'b8bc35012bf06f864b4813972d85'
            '5f070981bc07845e67a53782c198'
            '5d7ed8ca'
        ),
    },
}


def audit_recordings(root: Path) -> dict:
    root = Path(root).resolve()
    results = {}
    for kind, source in PROVENANCE.items():
        archive = (root / source["relative_dir"]).resolve()
        if not archive.is_relative_to(root) or not archive.is_dir():
            raise RecoveryEvidenceError("Missing/escaped v14 archived source")
        manifest_bytes = (archive / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != source["manifest_sha256"]:
            raise RecoveryEvidenceError(
                "v14 source manifest SHA differs from pinned provider artifact"
            )
        manifest = json.loads(manifest_bytes)
        a = audit(archive)
        b = replay(archive)
        if not (
            a["message_count"] == b["messages"] == source["messages"]
            and a["chunk_count"] == b["chunks"] == source["chunks"]
            and a["connection_count"]
            == len(manifest["connections"])
            == source["connections"]
            and a["books_final"] == b["books_final"]
            and manifest.get("required_duration_seconds") == 120
            and manifest.get("orders_enabled") is False
            and manifest.get("strategy_execution_enabled") is False
        ):
            raise RecoveryEvidenceError(
                "v14 source and independent replay are inconsistent"
            )
        for product in ("BTC-USD", "ETH-USD"):
            for field, value in b["per_product"][product].items():
                if a["per_product"][product].get(field, 0) != value:
                    raise RecoveryEvidenceError("Counter mismatch in v14 source replay")
        if kind == "clean":
            if not (
                a["status"] == "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
                and manifest.get("ready_for_research") is True
                and manifest.get("controlled_fault_injection") is None
                and manifest.get("faults") == []
                and manifest.get("total_reconnects") == 0
                and a["connection_sequences"] == {"0": [0, source["messages"] - 1]}
            ):
                raise RecoveryEvidenceError(
                    "Clean comparator source has incorrect admission"
                )
        else:
            injected = manifest.get("controlled_fault_injection")
            if not (
                a["status"] == "ARCHIVED_BUT_NOT_ADMITTED"
                and manifest.get("ready_for_research") is False
                and injected
                == {
                    "mode": "PUBLIC_FEED_DISCONNECT",
                    "after_messages": 180,
                    "never_admit": True,
                }
                and len(manifest["faults"]) == 1
                and manifest["faults"][0]["reason"]
                == "INJECTED_DISCONNECT_AFTER_COMMITTED_SOURCE"
                and manifest.get("total_reconnects") == 1
                and manifest["connections"][0]["integrity_fault"] is True
                and manifest["connections"][1]["integrity_fault"] is False
                and all(all(c["snapshots"].values()) for c in manifest["connections"])
                and a["connection_sequences"]
                == {"0": [0, 179], "1": [0, source["messages"] - 181]}
            ):
                raise RecoveryEvidenceError(
                    "Injected source not safely quarantined and reinitialized"
                )
        results[kind] = {
            "verified_as_archived_source": True,
            "source_message_count": source["messages"],
            "raw_chunk_count": source["chunks"],
            "connection_count": source["connections"],
            "admitted_as_continuous": kind == "clean",
            "source_manifest_sha256": source["manifest_sha256"],
            "github_actions_artifact_digest_sha256": source["artifact_sha256"],
            "connection_sequences": a["connection_sequences"],
            "per_product": a["per_product"],
            "full_book_sha256": {
                p: a["books_final"][p]["sha256"] for p in ("BTC-USD", "ETH-USD")
            },
        }
    return {
        "status": "CONTROLLED_PROVIDER_RECONNECT_SOURCE_DOUBLE_AUDITED",
        "verified": True,
        "source_type": "Coinbase Advanced Trade public WebSocket, no credentials",
        "provider_connection_closed_intentionally": True,
        "unplanned_network_failure_demonstrated": False,
        "gapped_capture_admitted_as_continuous": False,
        "independent_market_truth_verified": False,
        "orders_enabled": False,
        "executable_alpha_verified": False,
        "sessions": results,
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    try:
        result = audit_recordings(args.root)
    except (ValueError, KeyError, TypeError, OSError) as e:
        result = {
            "status": "REJECTED",
            "verified": False,
            "error": str(e)[:400],
            "orders_enabled": False,
        }
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    print(rendered)
    if args.output:
        args.output.write_text(rendered)
    if not result["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
