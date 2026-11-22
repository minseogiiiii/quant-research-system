"""Fail-closed public BTC/ETH capture supervision, with terminal dual-replay audit.

Examples:
  python scripts/v29_verified_capture.py --workdir /data/btceth --evidence-dir /data/evidence/run1 --mode smoke
  python scripts/v29_verified_capture.py --workdir /data/btceth --evidence-dir /data/evidence/run2 --mode 24h --execute-real-public-capture

Preflight alone NEVER means market capture succeeded. Each run needs a new
observer directory outside the collector workdir. No broker credentials/orders.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))


def _q14_load_after_path_setup_0():
    from v25_capture_admission import load_json
    from v25_capture_operator import exclusive_write
    from v26_capture_crosscheck import crosscheck
    from v26_capture_operator import run as operator_run
    from v29_capture_verdict import evaluate, sha
    from v29_ready_window import verify_ready_window

    return (
        load_json,
        exclusive_write,
        crosscheck,
        operator_run,
        evaluate,
        sha,
        verify_ready_window,
    )


(
    load_json,
    exclusive_write,
    crosscheck,
    operator_run,
    evaluate,
    sha,
    verify_ready_window,
) = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--evidence-dir", type=Path, required=True)
    p.add_argument("--mode", choices=["smoke", "24h"], default="smoke")
    p.add_argument("--execute-real-public-capture", action="store_true")
    p.add_argument("--raw-budget", type=int, default=None)
    p.add_argument("--archive-budget", type=int, default=None)
    p.add_argument("--disk-reserve", type=int, default=None)
    args = p.parse_args(argv)
    if args.raw_budget is None:
        args.raw_budget = 40_000_000_000 if args.mode == "24h" else 1_000_000_000
    if args.archive_budget is None:
        args.archive_budget = 55_000_000_000 if args.mode == "24h" else 2_000_000_000
    if args.disk_reserve is None:
        args.disk_reserve = 10 * 1024**3 if args.mode == "24h" else 1 * 1024**3
    observer = args.evidence_dir
    # Prevent even a BLOCKED verdict from writing into pre-existing evidence.
    if observer.exists() or observer.is_symlink():
        print("BLOCKED_EVIDENCE_DIRECTORY_MUST_NOT_EXIST", file=sys.stderr)
        return 2
    forwarded = [
        "--workdir",
        str(args.workdir),
        "--evidence-dir",
        str(observer),
        "--mode",
        args.mode,
        "--raw-budget",
        str(args.raw_budget),
        "--archive-budget",
        str(args.archive_budget),
        "--disk-reserve",
        str(args.disk_reserve),
    ]
    if args.execute_real_public_capture:
        forwarded.append("--execute-real-public-capture")
    code = None
    issue = None
    try:
        code = operator_run(forwarded)
    except (OSError, ValueError, RuntimeError, SystemExit, KeyboardInterrupt) as exc:
        issue = f"OPERATOR_EXCEPTION:{type(exc).__name__}:{str(exc)[:200]}"
        code = 2
    if not observer.is_dir() or observer.is_symlink():
        print("BLOCKED_NO_SAFE_OBSERVER_DIRECTORY", file=sys.stderr)
        return 2
    verdict_file = observer / "V29_FINAL_VERDICT.json"
    if verdict_file.exists() or verdict_file.is_symlink():
        print("BLOCKED_EXISTING_FINAL_VERDICT", file=sys.stderr)
        return 2
    preflight = operator = {}
    try:
        preflight = load_json(observer / "V26_PREFLIGHT.json")
    except (ValueError, OSError) as exc:
        issue = issue or f"PREFLIGHT_EVIDENCE_INVALID:{exc}"
    source = None
    evidence = observer / "V25_OPERATOR_EVIDENCE.json"
    try:
        operator = load_json(evidence)
        source = Path(operator["source_archive_dir"])
    except (ValueError, OSError, TypeError, KeyError) as exc:
        if args.execute_real_public_capture:
            issue = issue or f"OPERATOR_EVIDENCE_INVALID:{exc}"
    full_crosscheck = None
    ready_window = None
    if (
        args.execute_real_public_capture
        and code == 0
        and issue is None
        and source is not None
    ):
        try:
            full_crosscheck = crosscheck(source, observer)
            if full_crosscheck.get(
                "candidate_host_monitored_source"
            ) is True and full_crosscheck.get("source_manifest_sha256") == sha(
                source / "manifest.json"
            ):
                ready_window = verify_ready_window(
                    load_json(source / "manifest.json"), mode=args.mode
                )
                exclusive_write(observer / "V29_READY_WINDOW.json", ready_window)
            exclusive_write(observer / "V29_FULL_CROSSCHECK.json", full_crosscheck)
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            issue = (
                f"INDEPENDENT_CROSSCHECK_REJECTED:{type(exc).__name__}:{str(exc)[:220]}"
            )
    result = evaluate(
        preflight,
        operator,
        full_crosscheck,
        ready_window=ready_window,
        mode=args.mode,
        workdir=args.workdir,
        evidence_dir=observer,
        operator_exit_code=code,
        execution_requested=args.execute_real_public_capture,
    )
    result["supervisor_error"] = issue
    if issue is not None:
        result["problems"] = sorted(
            set(result["problems"] + ["SUPERVISOR_OR_CROSSCHECK_ERROR"])
        )
        result["status"] = "BLOCKED_FINAL_CAPTURE_GATE"
        result["candidate_host_monitored_internal_source"] = False
    result["evidence_digest_sha256"] = {
        f.name: sha(f)
        for f in [
            observer / "V26_PREFLIGHT.json",
            evidence,
            observer / "V26_TRACE_CONTEXT.json",
            observer / "V26_CLOCK_TRACE.jsonl",
            observer / "V29_FULL_CROSSCHECK.json",
            observer / "V29_READY_WINDOW.json",
        ]
        if f.is_file() and not f.is_symlink()
    }
    exclusive_write(verdict_file, result)
    print(result["status"] + ": " + ",".join(result["problems"]))
    return 0 if result["candidate_host_monitored_internal_source"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
