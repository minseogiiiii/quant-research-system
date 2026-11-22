"""Adversarial synthetic-only operational tests for R3 live engine + Gateway.

No archived source is promoted to LIVE. The clock and session are controlled
explicitly to isolate source-staleness and HTTP/SSE handshake races.
"""

import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

# Some frozen v34 modules prepend their own backend to sys.path.
# Explicitly keep the local candidate overlay ahead of frozen imports.
OVERLAY = str(Path(__file__).resolve().parents[1] / "backend")
sys.path.insert(0, OVERLAY)


def _q14_load_after_path_setup_0():
    from v36_live_engine import LiveMarketEngine

    return LiveMarketEngine


LiveMarketEngine = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

sys.path.insert(0, OVERLAY)


def _q14_load_after_path_setup_1():
    from v36_live_gateway import server_for_live

    return server_for_live


server_for_live = _q14_load_after_path_setup_1()
del _q14_load_after_path_setup_1

SESSION_A = "a" * 32
SESSION_B = "b" * 32


class OperationalRaceContract(unittest.TestCase):
    def test_product_l2_silence_with_backwards_wall_clock_fails_closed(self):
        clock = [1800000000000]
        mono = [10_000_000_000]
        e = LiveMarketEngine(
            mode="LIVE_SOURCE",
            collector_runtime_verified=True,
            collector_script_sha256="f" * 64,
            clock_ms=lambda: clock[0],
            monotonic_ns=lambda: mono[0],
        )
        with e._cv:
            e.status = "HEALTHY"
            e.reason = None
            e.session_id = SESSION_A
            e.last_local_mono = mono[0]
            e.last_heartbeat_local_mono = mono[0]
            for product in ("BTC-USD", "ETH-USD"):
                e.last_l2[product] = (clock[0], 3, "a" * 64, "10000000000")
                # The source L2 was last observed now, but a monotonic interval
                # of 6001ms without new L2 must invalidate the book.
                if hasattr(e, "last_l2_ingest_mono"):
                    e.last_l2_ingest_mono[product] = mono[0]
        mono[0] += 6_001_000_000
        clock[0] -= 20_000
        with e._cv:
            # Other channels continued to arrive, so global feed and heartbeat
            # recency cannot mask per-product L2 silence.
            e.last_local_mono = mono[0]
            e.last_heartbeat_local_mono = mono[0]
        self.assertEqual(e.health()["status"], "STALE")
        self.assertIsNone(e.snapshot("BTC-USD")["state"])

    def test_sse_source_and_first_snapshot_are_same_session_under_switch_race(self):
        e = LiveMarketEngine()
        with e._cv:
            e.status = "HEALTHY"
            e.session_id = SESSION_A
            e.rows["BTC-USD"].append(
                {
                    "state_version": 1,
                    "session_id": SESSION_A,
                    "product": "BTC-USD",
                    "mode": "ARCHIVE_SIMULATION",
                    "trading_enabled": False,
                }
            )
        original_health = e.health

        def racing_health():
            old = original_health()
            with e._cv:
                e.session_id = SESSION_B
                e.rows["BTC-USD"].clear()
                e.rows["BTC-USD"].append(
                    {
                        "state_version": 2,
                        "session_id": SESSION_B,
                        "product": "BTC-USD",
                        "mode": "ARCHIVE_SIMULATION",
                        "trading_enabled": False,
                    }
                )
            return old

        e.health = racing_health
        server = server_for_live(e)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/api/v36/stream?product=BTC-USD",
                timeout=4,
            ) as response:
                self.assertEqual(response.status, 200)
                events = []
                current = {}
                while len(events) < 2:
                    line = response.readline().decode().strip()
                    if not line:
                        if current:
                            events.append(current)
                            current = {}
                    elif line.startswith("event: "):
                        current["event"] = line[7:]
                    elif line.startswith("data: "):
                        current["data"] = json.loads(line[6:])
                    elif line.startswith("id: "):
                        current["id"] = line[4:]
                self.assertEqual([v["event"] for v in events], ["source", "snapshot"])
                self.assertEqual(
                    events[0]["data"]["session_id"], events[1]["data"]["session_id"]
                )
                self.assertTrue(
                    events[1]["id"].startswith(
                        "v36:" + events[1]["data"]["session_id"] + ":BTC-USD:"
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)


if __name__ == "__main__":
    unittest.main()
