"""Q29.2-H integrated load and process faults. Synthetic market feed only."""
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from q292_resilient_gateway import ManagedSource, ResilientRoute
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger, audit_ledger


class IntegratedStress(unittest.TestCase):
    def test_eight_streams_slow_header_and_repeated_source_loss(self):
        with tempfile.TemporaryDirectory(prefix="q292h_") as folder:
            root = Path(folder)
            p = ManagedSource("primary", root / "p", 90, True)
            s = ManagedSource("secondary", root / "s", 90, True)
            route = ResilientRoute(p, s)
            ledger = TransitionLedger(root / "route.jsonl")
            publisher = Publisher(route, ledger)
            server = BoundedServer(publisher, stream_seconds=20)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            publisher.start()
            thread.start()
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}"
            streams = []

            def until(check):
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    body = publisher.read()
                    if check(body):
                        return body
                    time.sleep(.05)
                self.fail("STRESS_DEADLINE")

            def snapshot():
                with urlopen(url + "/snapshot", timeout=2) as response:
                    return json.load(response)

            try:
                first = until(lambda x: x["active_source"] is not None
                              and set(x.get("checkpoint_comparison", {}).values())
                              <= {"MATCH", "NOT_COMPARABLE"}
                              and len(x.get("checkpoint_comparison", {})) == 2)
                if first["active_source"] == "secondary":
                    p, s = s, p
                for _ in range(8):
                    response = urlopen(url + "/events", timeout=8)
                    streams.append(response)
                    self.assertIn(b"id:", response.readline())
                    self.assertEqual(response.readline(), b"event: snapshot\n")
                    payload = json.loads(response.readline()[6:])
                    response.readline()
                    self.assertEqual(payload["source_session"], first["source_session"])
                with self.assertRaises(HTTPError) as error:
                    urlopen(url + "/events", timeout=2)
                self.assertEqual(error.exception.code, 503)
                error.exception.close()

                # An unterminated header must not hold a worker indefinitely.
                with socket.create_connection(("127.0.0.1", port), timeout=2) as slow:
                    slow.sendall(b"GET /snapshot HTTP/1.1\r\nHost: ")
                    time.sleep(.4)
                    slow.sendall(b"x")
                    time.sleep(.5)
                    self.assertEqual(slow.recv(1), b"")
                with ThreadPoolExecutor(max_workers=3) as pool:
                    replies = list(pool.map(lambda _: snapshot(), range(30)))
                self.assertTrue(all(r["source_session"] == first["source_session"]
                                    for r in replies))

                p.worker.process.kill()
                second = until(lambda x: x["active_source"] == s.name)
                self.assertNotEqual(first["source_session"], second["source_session"])
                for response in streams:
                    tail = response.read()
                    self.assertIn(b"event: resync_required", tail)
                    self.assertNotIn(
                        second["source_session"].encode() + b'","state_version"', tail)
                    response.close()
                streams.clear()
                until(lambda x: x.get("source_attempts", {}).get(p.name, 0) >= 2
                      and x.get("checkpoint_comparison", {}).get("BTC-USD")
                      in ("MATCH", "NOT_COMPARABLE"))
                s.worker.process.kill()
                third = until(lambda x: x["active_source"] == p.name)
                self.assertNotEqual(first["source_session"], third["source_session"])
                self.assertFalse(third["zero_gap_certified"])
                self.assertFalse(third["historical_gap_recovered"])
                self.assertGreater(third["generation"], second["generation"])
                self.assertFalse(publisher.failure)
            finally:
                for response in streams:
                    response.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                publisher.close()
                p.close()
                s.close()
                ledger.close()
            audit = audit_ledger(root / "route.jsonl")
            self.assertGreaterEqual(audit["records"], 3)
            self.assertFalse(audit["torn_tail"])


if __name__ == "__main__":
    unittest.main()
