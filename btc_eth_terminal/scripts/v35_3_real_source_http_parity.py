"""Exhaustive real Coinbase source-backed HTTP/SSE parity, NOT browser DOM E2E."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Thread

from v35_3_sse_soak import CLEAN, endpoint, events, pin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from v35_1_frontend_integrity import CANDIDATE_CSS, CANDIDATE_JS, candidate_asset
from v35_replay_service import replay_capture
from v35_unified_gateway import server_for


def run():
    manifest = pin(CLEAN)
    ds = replay_capture(CLEAN, manifest)
    server = server_for(ds)
    t = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results = {
        "schema": "v35_3_real_archive_http_sse_full_state_parity_v1",
        "source_manifest_sha256": manifest,
        "source_messages": ds.message_count,
        "state_digest": ds.final_state_sha256,
        "mode": "ARCHIVE_REPLAY",
        "market_data_mocked": False,
        "trading_enabled": False,
        "products": {},
    }
    try:
        for path in ["/research", "/terminal", "/research/v35.1"]:
            code, body, _ = endpoint(base, path)
            assert code == 200 and body
        for asset in [CANDIDATE_CSS, CANDIDATE_JS]:
            code, wire, _ = endpoint(base, asset)
            assert code == 200 and wire == candidate_asset(asset)[0]
        for prod in ["BTC-USD", "ETH-USD"]:
            expected = list(ds.rows[prod])
            got = []
            after = 0
            while True:
                code, raw, _ = endpoint(
                    base, f"/api/v35/history?product={prod}&limit=45&after={after}"
                )
                assert code == 200
                page = json.loads(raw)
                assert (
                    page["source_manifest_sha256"] == manifest
                    and page["mode"] == "ARCHIVE_REPLAY"
                )
                got += page["rows"]
                after = page["next_after"]
                if not page["has_more"]:
                    break
            assert got == expected, prod + " HTTP history loss/reorder"
            sse = []
            cursor = None
            while True:
                header = {} if cursor is None else {"Last-Event-ID": cursor}
                code, raw, _ = endpoint(
                    base, f"/api/v35/stream?product={prod}&limit=35", headers=header
                )
                assert code == 200
                chunks = events(raw)
                assert (
                    chunks[0]["name"] == "source" and chunks[-1]["name"] == "replay_end"
                )
                states = [c for c in chunks if c["name"] == "state"]
                sse += [x["data"] for x in states]
                for state in states:
                    assert (
                        state["id"]
                        == f"{manifest}:{prod}:{state['data']['state_version']}"
                    )
                if not chunks[-1]["data"]["has_more"]:
                    break
                cursor = states[-1]["id"]
            assert sse == expected, prod + " SSE history loss/reorder"
            at = [expected[0], expected[len(expected) // 2], expected[-1]]
            for exp in at:
                code, raw, _ = endpoint(
                    base,
                    f"/api/v35/snapshot?product={prod}&version={exp['state_version']}",
                )
                assert code == 200 and json.loads(raw)["state"] == exp, (
                    "Snapshot mismatch"
                )
            code, raw, _ = endpoint(base, f"/api/v35/candles?product={prod}")
            assert code == 200
            candles = json.loads(raw)
            assert candles["source_manifest_sha256"] == manifest
            assert candles["rows"] == list(ds.candles[prod]), (
                "Candle HTTP source mismatch"
            )
            for state in expected:
                for bar in candles["rows"]:
                    if bar["available_at"] > state["receiver_time_ms"]:
                        assert bar not in [
                            c
                            for c in candles["rows"]
                            if c["available_at"] <= state["receiver_time_ms"]
                        ]
            results["products"][prod] = {
                "market_state_rows_verified": len(expected),
                "sse_state_rows_verified": len(sse),
                "snapshots_verified": len(at),
                "candles_verified": len(candles["rows"]),
                "checkpoint_prices": [
                    {
                        "state_version": row["state_version"],
                        "receiver_time_ms": row["receiver_time_ms"],
                        "best_bid": row.get("best_bid"),
                        "best_ask": row.get("best_ask"),
                        "spread": row.get("spread"),
                    }
                    for row in at
                ],
            }
        results["status"] = "PASS_FULL_SOURCE_BACKED_HTTP_SSE_PARITY"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)
    return results


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--report", required=True, type=Path)
    o = p.parse_args()
    r = run()
    o.report.write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["status"].startswith("PASS") else 1)
