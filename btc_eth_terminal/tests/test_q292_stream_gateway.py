"""Synthetic publication, ledger and real HTTP persistent stream contracts."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger


def body(gen, active="primary"):
    return {"gateway_run": "a" * 32, "generation": gen,
            "source_session": ("b" if active == "primary" else "c") * 32,
            "active_source": active, "status": "HEALTHY",
            "states": {"BTC-USD": {"source_last_l2_receiver_ms":
                                   time.time_ns() // 1_000_000}},
            "real_orders_enabled": False, "zero_gap_certified": False}


class Route:
    run = "a" * 32

    def __init__(self):
        self.generation = 1

    def read(self):
        return body(self.generation, "primary" if self.generation == 1
                    else "secondary")


class StreamContracts(unittest.TestCase):
    def test_durable_before_publish_and_disk_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "route.jsonl"
            ledger = TransitionLedger(path)
            try:
                ledger.commit(body(1))
                self.assertEqual(audit_ledger(path)["records"], 1)
                with patch("q292_stream_gateway.os.fsync", side_effect=OSError):
                    publisher = Publisher(Route(), ledger)
                    publisher.route.generation = 2
                    publisher.start()
                    publisher.thread.join(timeout=3)
                    self.assertTrue(publisher.failure)
                    self.assertIsNone(publisher.read()["states"])
                    publisher.close()
            finally:
                ledger.close()

    def test_persistent_sse_invalidates_on_generation_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "route.jsonl"
            ledger = TransitionLedger(path)
            route = Route()
            publisher = Publisher(route, ledger)
            publisher.start()
            server = BoundedServer(publisher, stream_seconds=8)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_address[1])
            try:
                end = time.monotonic() + 3
                while publisher.read()["states"] is None:
                    self.assertLess(time.monotonic(), end)
                    time.sleep(.05)
                with urlopen(url + "/events", timeout=4) as response:
                    first = response.readline()
                    self.assertIn(b":1:", first)
                    self.assertEqual(response.readline(), b"event: snapshot\n")
                    self.assertIn(b"data:", response.readline())
                    response.readline()
                    # An active streaming request cannot block snapshot access.
                    with urlopen(url + "/snapshot", timeout=2) as snap:
                        self.assertEqual(json.load(snap)["generation"], 1)
                    route.generation = 2
                    remaining = response.read()
                    self.assertIn(b"event: resync_required", remaining)
                    self.assertNotIn(b'"active_source":"secondary","status":"HEALTHY"',
                                     remaining)
                self.assertEqual(audit_ledger(path)["records"], 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                publisher.close()
                ledger.close()

    def test_torn_tail_and_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "route.jsonl"
            ledger = TransitionLedger(path)
            ledger.commit(body(1))
            ledger.close()
            original = path.read_bytes()
            path.write_bytes(original + b'{"partial":')
            self.assertTrue(audit_ledger(path)["torn_tail"])
            path.write_bytes(original.replace(b'"generation":1', b'"generation":2'))
            with self.assertRaises(ValueError):
                audit_ledger(path)


if __name__ == "__main__":
    unittest.main()
