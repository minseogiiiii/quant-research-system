"""Independent acceptance gate for one or more Coinbase public-source archives.

Archives are NEVER physically stitched. A new session/connection is an explicit
boundary regardless of whether adjacent local wall-clock intervals overlap.
Only an independently dual-replayed, uninterrupted single source with >=24h
elapsed may receive the narrow ONE_DAY_INTERNAL_CAPTURE certification.
No trusted exchange clock, external book, order acknowledgement, or fills.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from audit_soak import audit as primary_replay
from independent_soak_replay import replay as independent_replay


class ContinuityError(ValueError):
    pass


def timestamp(s):
    if not isinstance(s, str) or not s.endswith("Z"):
        raise ContinuityError("Not an explicit UTC timestamp")
    d = datetime.fromisoformat(s[:-1] + "+00:00")
    if d.tzinfo is None or d.utcoffset().total_seconds() != 0:
        raise ContinuityError("Non-UTC timestamp")
    return d


def validate_intervals(rows):
    if not rows:
        raise ContinuityError("No source observations supplied")
    seen = set()
    ordered = []
    for row in rows:
        if row["manifest_sha256"] in seen:
            raise ContinuityError("Duplicate original source capture in cohort")
        seen.add(row["manifest_sha256"])
        a = timestamp(row["sampled_from_utc"])
        b = timestamp(row["finished_at_utc"])
        if not a < b or row["single_connection"] is not True or row["fault_count"] != 0:
            raise ContinuityError("Non-admissible or gapped source in cohort")
        if (
            row["primary_accepted"] is not True
            or row["secondary_replay_accepted"] is not True
        ):
            raise ContinuityError("An archive failed independent dual-replay")
        ordered.append((a, b, row))
    ordered.sort(key=lambda r: r[0])
    boundaries = []
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = (nxt[0] - prev[1]).total_seconds()
        boundaries.append(
            {
                "left_source_manifest_sha256": prev[2]["manifest_sha256"],
                "right_source_manifest_sha256": nxt[2]["manifest_sha256"],
                "wall_clock_gap_seconds": gap,
                "has_unreconciled_connection_boundary": True,
                "continuous_trading_or_training_join_allowed": False,
                "reason": "Separate sessions never establish exchange event continuity, even when wall-clock gap <=0",
            }
        )
    single_24h = (
        len(ordered) == 1
        and (ordered[0][1] - ordered[0][0]).total_seconds() >= 86400
        and ordered[0][2]["required_duration_seconds"] >= 86400
    )
    return {
        "status": "ONE_DAY_SINGLE_CONNECTION_INTERNALLY_REPLAYED"
        if single_24h
        else "SOURCE_COHORT_REPLAYED_BUT_NO_24H_CONTINUOUS_PROOF",
        "sessions": len(ordered),
        "total_session_wall_seconds": sum(
            (r[1] - r[0]).total_seconds() for r in ordered
        ),
        "single_connection_24h_internal_evidence": single_24h,
        "uninterrupted_multiday_across_separate_sessions_proven": False,
        "independently_time_aligned_exchange_truth_proven": False,
        "host_clock_calibrated": False,
        "boundaries": boundaries,
        "source_records": [
            {
                "path": x[2]["path"],
                "manifest_sha256": x[2]["manifest_sha256"],
                "sampled_from_utc": x[2]["sampled_from_utc"],
                "finished_at_utc": x[2]["finished_at_utc"],
                "message_count": x[2]["message_count"],
                "chunks": x[2]["chunks"],
                "producer_release": x[2]["producer_release"],
            }
            for x in ordered
        ],
        "execution_alpha_or_fills_verified": False,
        "live_orders_placed": False,
    }


def audit_cohort(roots):
    raw = []
    for source in roots:
        path = Path(source)
        if path.is_symlink() or not path.is_dir():
            raise ContinuityError("Unsafe/missing source directory")
        mf_path = path / "manifest.json"
        if mf_path.is_symlink() or not mf_path.is_file():
            raise ContinuityError("Missing/symlinked manifest")
        manifest_bytes = mf_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        main = primary_replay(path)
        secondary = independent_replay(path)
        if main["status"] != "INTERNALLY_REPLAYED_NO_KNOWN_GAP":
            raise ContinuityError("Primary replay refused gapped/quarantined capture")
        if (
            secondary["messages"] != main["message_count"]
            or secondary["chunks"] != main["chunk_count"]
        ):
            raise ContinuityError("Independent original source replay disagrees")
        # A dual replay agrees on code interpretation, not separate venue truth.
        for product in ("BTC-USD", "ETH-USD"):
            if (
                main["books_final"][product]["sha256"]
                != secondary["books_final"][product]["sha256"]
            ):
                raise ContinuityError("Independent L2 final-book digest mismatch")
        if mf_path.read_bytes() != manifest_bytes:
            raise ContinuityError("Manifest changed after dual-replay")
        raw.append(
            {
                "path": str(path.resolve()),
                "manifest_sha256": sha256(manifest_bytes).hexdigest(),
                "sampled_from_utc": manifest["sampled_from_utc"],
                "finished_at_utc": manifest["finished_at_utc"],
                "required_duration_seconds": manifest["required_duration_seconds"],
                "single_connection": len(manifest["connections"]) == 1,
                "fault_count": len(manifest["faults"]),
                "primary_accepted": True,
                "secondary_replay_accepted": True,
                "message_count": main["message_count"],
                "chunks": main["chunk_count"],
                "producer_release": manifest.get("producer_release") or "legacy",
            }
        )
    return validate_intervals(raw)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("sources", type=Path, nargs="+")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        result = audit_cohort(args.sources)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        result = {
            "status": "REJECTED_SOURCE_COHORT_QUARANTINED",
            "reason": str(exc)[:500],
            "research_join_allowed": False,
            "actual_24h_verified": False,
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit(2)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "sessions": result["sessions"],
                "single_24h_evidence": result[
                    "single_connection_24h_internal_evidence"
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
