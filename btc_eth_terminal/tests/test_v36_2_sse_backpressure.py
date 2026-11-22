"""Synthetic-only bounded SSE memory and source-qualified cursor eviction."""

import sys
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from v36_live_gateway import server_for_live

    return server_for_live


server_for_live = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

sys.path.insert(0, str(ROOT / "tests"))


def _q14_load_after_path_setup_1():
    from test_v36_ci_synthetic_admission import boot, line

    return boot, line


boot, line = _q14_load_after_path_setup_1()
del _q14_load_after_path_setup_1


class BoundedSSE(unittest.TestCase):
    def serve(self, engine):
        server = server_for_live(engine)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(
            lambda: (server.shutdown(), server.server_close(), t.join(timeout=4))
        )
        return server

    def test_evicted_version_cannot_be_silently_skipped(self):
        engine, _ = boot()
        prior = engine.rows["BTC-USD"][-1]
        base = datetime(2026, 11, 14, tzinfo=UTC)
        for seq in range(4, 400):
            e = {
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [
                    {
                        "side": "bid",
                        "price_level": "100",
                        "new_quantity": str(seq % 4 + 1),
                    }
                ],
            }
            engine.consume(
                line(seq, "l2_data", [e], base=base + timedelta(milliseconds=950 * seq))
            )
        self.assertEqual(engine.health()["status"], "HEALTHY")
        self.assertLessEqual(len(engine.rows["BTC-USD"]), 256)
        with self.assertRaisesRegex(Exception, "RESYNC_REQUIRED"):
            engine.poll("BTC-USD", prior["session_id"], prior["state_version"], 0.001)
        server = self.serve(engine)
        old = f"v36:{prior['session_id']}:BTC-USD:{prior['state_version']}"
        req = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/v36/stream?product=BTC-USD",
            headers={"Last-Event-ID": old},
        )
        with self.assertRaises(HTTPError) as cm:
            urlopen(req, timeout=4)
        self.assertEqual(cm.exception.code, 409)

    def test_eight_client_limit_and_reclaim_after_disconnect(self):
        engine, _ = boot()
        server = self.serve(engine)
        url = f"http://127.0.0.1:{
            server.server_address[1]
        }/api/v36/stream?product=BTC-USD"
        opened = []
        try:
            for _ in range(8):
                stream = urlopen(url, timeout=4)
                self.assertEqual(stream.status, 200)
                opened.append(stream)
            limit = time.monotonic() + 2
            while (
                time.monotonic() < limit and server.metrics()["active_connections"] < 8
            ):
                time.sleep(0.02)
            self.assertEqual(server.metrics()["active_connections"], 8)
            try:
                extra = urlopen(url, timeout=3)
                extra.close()
                self.fail("Ninth concurrent live SSE slot must not be allocated")
            except (ConnectionError, OSError):
                pass
            self.assertLessEqual(server.metrics()["peak_connections"], 10)
        finally:
            for c in opened:
                c.close()
            engine.transport_lost("SYNTHETIC_STOP")
            until = time.monotonic() + 5
            while time.monotonic() < until and server.metrics()["active_connections"]:
                time.sleep(0.05)
            self.assertEqual(server.metrics()["active_connections"], 0)
            self.assertGreaterEqual(server.metrics()["sse_rejected_slots"], 1)


if __name__ == "__main__":
    unittest.main()
