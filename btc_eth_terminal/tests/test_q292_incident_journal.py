"""Synthetic independent-journal contracts; never live acceptance."""
import errno
import json
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
import unittest
from pathlib import Path
from unittest.mock import patch

from q292_incident_journal import (
    Journal, JournalProcess, audit, read_journal, safe_source,
)


def source(connection=0, gaps=0, messages=100, seq=99, heartbeats=20):
    return {"status": "HEALTHY", "session_sha256": str(connection) * 64,
            "connection": connection, "gaps": gaps, "messages": messages,
            "sequence": seq, "heartbeats": heartbeats, "receiver_age_ms": 3,
            "ready": True}


class JournalContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "journal.jsonl"

    def test_reconnect_not_false_counter_regression(self):
        j = Journal(self.file)
        try:
            j.append("START")
            j.append("SAMPLE", source())
            j.append("UNAVAILABLE")
            j.append("SAMPLE", source(1, 1, 103, 2, 1))
            j.append("STOP")
        finally:
            j.close()
        r = audit(*read_journal(self.file))
        self.assertTrue(r["clean_stop"])
        self.assertEqual(r["anomalies"], 0)
        self.assertEqual(r["unavailable_samples"], 1)
        self.assertTrue(r["observed_epoch_transitions"][0]["transition_consistent"])
        self.assertFalse(r["zero_gap_certified"])
        self.assertFalse(r["resilience_certified"])

    def test_same_epoch_counter_regression_is_anomaly(self):
        j = Journal(self.file)
        try:
            j.append("START")
            j.append("SAMPLE", source())
            j.append("SAMPLE", source(messages=99, seq=98))
        finally:
            j.close()
        self.assertEqual(audit(*read_journal(self.file))["anomalies"], 1)

    def test_disk_full_and_torn_record_not_clean(self):
        j = Journal(self.file)
        try:
            j.append("START")
            with (
                patch("q292_incident_journal.os.write",
                      side_effect=OSError(errno.ENOSPC, "disk full")),
                self.assertRaises(OSError),
            ):
                j.append("SAMPLE", source())
            with self.assertRaises(ValueError):
                j.append("STOP")
        finally:
            j.close()
        with self.file.open("ab") as f:
            f.write(b'{"partial":')
        r = audit(*read_journal(self.file))
        self.assertTrue(r["torn_tail"])
        self.assertFalse(r["clean_stop"])
        self.assertEqual(r["records"], 1)

    def test_mutation_rejected_and_unknown_fields_never_written(self):
        j = Journal(self.file)
        try:
            j.append("START")
            bad = {**source(), "raw": "PRIVATE"}
            with self.assertRaises(ValueError):
                j.append("SAMPLE", bad)
            j.append("SAMPLE", source())
        finally:
            j.close()
        text = self.file.read_text()
        self.assertNotIn("PRIVATE", text)
        self.file.write_text(text.replace('"messages":100', '"messages":101'))
        with self.assertRaises(ValueError):
            read_journal(self.file)

    def test_actual_independent_process_sigkill_preserves_records(self):
        script = (Path(__file__).resolve().parents[1]
                  / "scripts/q292_incident_journal.py")
        p = subprocess.Popen([sys.executable, str(script), "--socket",
                              str(self.root / "absent.sock"), "--journal",
                              str(self.file), "--duration", "30", "--period", "1"])
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if self.file.exists() and b"UNAVAILABLE" in self.file.read_bytes():
                    break
                time.sleep(.05)
            else:
                self.fail("independent monitor did not record unavailable source")
            p.send_signal(signal.SIGKILL)
            p.wait(timeout=5)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        r = audit(*read_journal(self.file))
        self.assertFalse(r["clean_stop"])
        self.assertGreaterEqual(r["unavailable_samples"], 1)

    def test_owner_lifecycle_persists_unavailable_evidence_not_acceptance(self):
        public = self.root / "public"
        public.mkdir()
        p = JournalProcess(self.root / "capture", public, 180)
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if p.path.exists() and b"UNAVAILABLE" in p.path.read_bytes():
                    break
                time.sleep(.05)
            else:
                self.fail("journal process did not start")
            p.check()
            r = p.finish()
            self.assertTrue(r["clean_stop"])
            self.assertEqual(r["ready_samples"], 0)
            self.assertEqual(r, json.loads(p.public.read_text()))
            with self.assertRaises(ValueError):
                p.require_clean()
        finally:
            if p.process.poll() is None:
                p.process.kill()
                p.process.wait()

    def test_independent_process_observes_actual_unix_http_epoch_change(self):
        work = self.root / "capture"
        work.mkdir(mode=0o700)
        public = self.root / "public"
        public.mkdir()
        state = {"connection": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                c = state["connection"]
                common = {"trading_enabled": False, "execution_authorized": False}
                if self.path == "/api/v36/health":
                    body = {**common, "mode": "LIVE_SOURCE", "status": "HEALTHY",
                            "operationally_healthy": True, "live_feed": True,
                            "q25_isolation_schema": "q25_source_epoch_isolation_v1",
                            "snapshots": ["BTC-USD", "ETH-USD"],
                            "subscriptions": ["heartbeats", "level2", "market_trades"],
                            "session_id": str(c) * 32, "connection_id": c,
                            "connection_gaps": c, "messages": 100 + c * 3,
                            "last_sequence": 99 if c == 0 else 2,
                            "heartbeats": 20 if c == 0 else 1,
                            "last_receiver_time_ms": time.time_ns() // 1_000_000}
                else:
                    body = {**common, "monitor_transport": "UNIX_SOCKET"}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        sock = work / "v36_monitor.sock"
        server = socketserver.UnixStreamServer(str(sock), Handler)
        os.chmod(sock, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        p = None

        def wait_for(marker):
            end = time.monotonic() + 8
            while time.monotonic() < end:
                if p.path.exists() and marker in p.path.read_bytes():
                    return
                time.sleep(.05)
            self.fail("missing independent journal observation")

        try:
            p = JournalProcess(work, public, 180)
            wait_for(b'"connection":0')
            state["connection"] = 1
            wait_for(b'"connection":1')
            r = p.require_clean()
            self.assertEqual(r["anomalies"], 0)
            self.assertEqual(len(r["observed_epoch_transitions"]), 1)
            self.assertFalse(r["zero_gap_certified"])
            self.assertFalse(r["resilience_certified"])
        finally:
            if p is not None and p.process.poll() is None:
                p.process.kill()
                p.process.wait()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_no_source_or_orders_cannot_be_ready(self):
        value = safe_source({"reason": "private secret"}, {}, 503, 503)
        self.assertFalse(value["ready"])
        self.assertNotIn("secret", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
