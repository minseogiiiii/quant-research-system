"""Run deterministic isolated example. Never reads historical market data or sends orders."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    from v24_independent_fixture_audit import audit_fixture
    from v24_paper_oms import FixturePaperOMS
    from v24_paper_risk import FIXTURE, FixtureQuote, PaperPolicy
    return audit_fixture, FixturePaperOMS, FIXTURE, FixtureQuote, PaperPolicy

audit_fixture, FixturePaperOMS, FIXTURE, FixtureQuote, PaperPolicy = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


def quotes(t, btc_bid="100", btc_ask="101", eth_bid="50", eth_ask="51"):
    return {
        s: FixtureQuote(
            instrument=s, bid=bid, ask=ask, received_ms=t, decision_ms=t, origin=FIXTURE
        )
        for s, bid, ask in (
            ("BTC-USD", btc_bid, btc_ask),
            ("ETH-USD", eth_bid, eth_ask),
        )
    }


def run(db_path: Path):
    if db_path.exists():
        raise ValueError("Refuse to overwrite existing fixture ledger")
    oms = FixturePaperOMS(db_path, PaperPolicy())
    oms.mark("fixture:mark1", quotes(1000))
    oms.risk_mode(
        "fixture:arm1",
        "ARM",
        reason="Manual synthetic fixture demonstration",
        at_ms=1000,
        manual=True,
    )
    oms.submit(
        "fixture:intent1",
        "T1",
        instrument="BTC-USD",
        side="BUY",
        quantity="2",
        limit_price="100",
        quote=quotes(1000)["BTC-USD"],
    )
    oms.lifecycle("fixture:ack1", "ACK", "T1", at_ms=1001)
    oms.lifecycle("fixture:fill1", "FILL", "T1", at_ms=1002, quantity="0.5", price="99")
    oms.lifecycle("fixture:cancel1", "CANCEL_REQUEST", "T1", at_ms=1003)
    oms.lifecycle(
        "fixture:fill2", "FILL", "T1", at_ms=1004, quantity="0.25", price="99.5"
    )
    oms.lifecycle("fixture:cancel_ack1", "CANCEL_ACK", "T1", at_ms=1005)
    oms.mark("fixture:mark2", quotes(1006, btc_bid="100.5", btc_ask="101.5"))
    oms.risk_mode(
        "fixture:halt1",
        "HALT",
        reason="Manual synthetic fail-safe demonstration",
        at_ms=1007,
        manual=True,
    )
    final = oms.snapshot()
    audit = audit_fixture(db_path)
    if (
        final["cash_usd"] != audit["cash_usd"]
        or final["positions"] != audit["positions"]
        or final["mode"] != audit["mode"]
        or final["event_count"] != audit["event_count"]
        or final["unanchored_local_event_chain_sha256"]
        != audit["same_original_event_chain_sha256"]
        or any(
            v["status"] != audit["orders"][k]["status"]
            for k, v in final["orders"].items()
        )
    ):
        raise RuntimeError("Independent fixture state divergence")
    return {
        "schema": "V24_SYNTHETIC_FIXTURE_REPORT_V1",
        "source_role": "GENERATED_SYNTHETIC_EVENTS_ONLY_NOT_MARKET_SOURCE",
        "evidence_status": "ENGINEERING_TEST_ONLY_NOT_P3_OR_ALPHA",
        "state": final,
        "independent_audit": audit,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    x = ap.parse_args()
    report = run(x.db)
    x.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": report["evidence_status"],
                "independent_audit": report["independent_audit"]["status"],
                "events": report["state"]["event_count"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
