"""Actual two original collector PIDs with synthetic transport; no live claim."""
import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from q292_dual_gateway import Collector, DualRoute, cursor, server_for_dual


class DualGatewayContracts(unittest.TestCase):
    def test_two_real_processes_primary_kill_then_secondary_then_total_loss(self):
        with tempfile.TemporaryDirectory(prefix="q292dual_") as name:
            root = Path(name)
            sources = []
            server = thread = None
            try:
                for label in ("primary", "secondary"):
                    sources.append(Collector(label, root / label, 30, True))
                p, s = sources
                self.assertNotEqual(p.process.pid, s.process.pid)
                self.assertNotEqual(p.work, s.work)
                route = DualRoute(p, s)
                server = server_for_dual(route)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = "http://127.0.0.1:" + str(server.server_address[1])

                def get(path="/snapshot", last=None):
                    req = Request(url + path, headers=(
                        {"Last-Event-ID": last} if last else {}))
                    try:
                        with urlopen(req, timeout=3) as response:
                            return response.status, json.load(response)
                    except HTTPError as exc:
                        return exc.code, json.load(exc)

                def until(label):
                    end = time.monotonic() + 12
                    while time.monotonic() < end:
                        status, value = get()
                        if status == 200 and value["active_source"] == label:
                            return value
                        time.sleep(.15)
                    self.fail("DUAL_SOURCE_NOT_READY:" + label)

                first = until("primary")
                self.assertEqual(first["mode"], "SYNTHETIC_ONLY")
                self.assertEqual(set(first["states"]), {"BTC-USD", "ETH-USD"})
                self.assertTrue(all(
                    row["session_id"] == first["source_session"]
                    for row in first["states"].values()))
                old = cursor(first)
                p.process.send_signal(signal.SIGKILL)
                p.process.wait(timeout=3)
                second = until("secondary")
                self.assertNotEqual(first["source_session"], second["source_session"])
                self.assertGreater(second["generation"], first["generation"])
                status, invalid = get(last=old)
                self.assertEqual(status, 409)
                self.assertIsNone(invalid["states"])
                with urlopen(Request(url + "/events", headers={
                        "Last-Event-ID": old}), timeout=3) as response:
                    self.assertIn(b"event: resync_required", response.read())
                self.assertFalse(second["zero_gap_certified"])
                self.assertFalse(second["historical_gap_recovered"])
                s.process.send_signal(signal.SIGKILL)
                s.process.wait(timeout=3)
                status, body = get()
                self.assertEqual(status, 503)
                self.assertIsNone(body["states"])
                self.assertFalse(body["real_orders_enabled"])
                for child in sources:
                    self.assertTrue((child.work / "soak_evidence").is_dir())
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=3)
                for child in sources:
                    child.close()


if __name__ == "__main__":
    unittest.main()
