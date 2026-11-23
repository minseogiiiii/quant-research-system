"""Q27 P0-D: real-compatible Node preload, no-heartbeat fault and privacy.

Only SYNTHETIC Coinbase-like transport is faulted. Genuine public Coinbase
180s is an independent read-only pipeline gate with guard opt-in.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]

from q25_session_isolation_guard import SourceEpochIsolatedEngine  # noqa: E402
from q27_heartbeat_receipt import (  # noqa: E402
    Q27TraceRejected,
    build_receipt,
    read_trace,
)
from test_q26_three_fault_recovery import ThreeFaultRecovery  # noqa: E402
from v36_observe_existing_collector import read_tap  # noqa: E402


class Q27PrivacyContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="q27_privacy_")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "q27_private.jsonl"

    def fixture(self):
        def row(n, event, socket_index, heartbeat_count=0):
            return {
                "schema": "q27_heartbeat_guard_event_v1",
                "index": n,
                "event": event,
                "socket_index": socket_index,
                "elapsed_ms": n * 100,
                "socket_age_ms": n * 100 if socket_index is not None else None,
                "last_heartbeat_age_ms": 0 if heartbeat_count else None,
                "heartbeat_count": heartbeat_count,
                "other_message_count": n if socket_index is not None else 0,
                "timeout_threshold_ms": 4500,
                "startup_grace_ms": 9000,
                "interventions_total": 0,
                "synthetic_fixture": True,
                "real_orders_enabled": False,
                "native_ping_pong_visible": False,
            }

        return [
            row(0, "GUARD_START", None),
            row(1, "SOCKET_CONSTRUCTED", 0),
            row(2, "SOCKET_OPEN", 0),
            row(3, "FIRST_HEARTBEAT", 0, 1),
            row(4, "SOCKET_CLOSE", 0, 2),
            row(5, "PROCESS_EXIT", 0, 2),
        ]

    def write(self, values):
        self.path.write_text(
            "".join(json.dumps(x) + "\n" for x in values)
        )
        os.chmod(self.path, 0o600)

    def test_strict_bounded_clean_receipt(self):
        self.write(self.fixture())
        x = build_receipt(self.path)
        self.assertTrue(x["trace_schema_verified"])
        self.assertEqual(x["opened_sockets_constructed"], 1)
        self.assertEqual(x["sockets_with_first_heartbeat"], 1)
        self.assertEqual(x["interventions_total"], 0)
        self.assertFalse(x["real_orders_enabled"])
        self.assertFalse(x["native_ping_pong_control_frames_available"])

    def test_reject_extra_secrets_urls_prices_or_error_text(self):
        for key, value in [
            ("private_api_key", "SECRET"),
            ("source_url", "wss://secret"),
            ("price", 999),
            ("error_message", "PRIVATE_HOST"),
            ("close_reason", "PRIVATE_REASON"),
        ]:
            with self.subTest(key=key):
                rows = self.fixture()
                rows[4][key] = value
                self.write(rows)
                with self.assertRaises(Q27TraceRejected):
                    build_receipt(self.path)

    def test_counter_regression_forgeries_truncation_permissions(self):
        for field, value in [
            ("index", 17),
            ("elapsed_ms", 1),
            ("interventions_total", 1),
            ("timeout_threshold_ms", 9999),
            ("heartbeat_count", -1),
            ("real_orders_enabled", True),
            ("native_ping_pong_visible", True),
        ]:
            with self.subTest(field=field):
                rows = self.fixture()
                rows[4][field] = value
                self.write(rows)
                with self.assertRaises(Q27TraceRejected):
                    read_trace(self.path)
        self.write(self.fixture())
        os.chmod(self.path, 0o644)
        with self.assertRaises(Q27TraceRejected):
            read_trace(self.path)
        os.chmod(self.path, 0o600)
        symlink = self.path.with_name("link")
        symlink.symlink_to(self.path)
        with self.assertRaises(Q27TraceRejected):
            read_trace(symlink)
        symlink.unlink()
        hardlink = self.path.with_name("hardlink")
        os.link(self.path, hardlink)
        with self.assertRaises(Q27TraceRejected):
            read_trace(self.path)
        hardlink.unlink()
        with self.path.open("ab") as stream:
            stream.write(b'{"incomplete":')
        with self.assertRaises(Q27TraceRejected):
            read_trace(self.path)

    def test_opt_in_mandatory_for_preload(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SOAK_TEST_FIXTURE": "1",
            "Q27_HEARTBEAT_TRACE_PRIVATE": str(self.path),
        }
        result = subprocess.run([
            node, "--import",
            str(ROOT / "scripts/q27_heartbeat_guard.mjs"),
            "-e", "process.exitCode=0",
        ], cwd=self.temp.name, env=env, capture_output=True, timeout=4)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.path.exists())


class Q27OriginalCollectorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node unavailable")

    def test_heartbeat_stale_with_continuous_l2_recovers_actual_guard(self):
        existing = ThreeFaultRecovery(
            "test_heartbeat_only_l2_continues_with_synthetic_watchdog"
        )
        summary = existing.run_scenario(
            "HEARTBEAT_ONLY_SILENCE", use_q27=True
        )
        self.assertTrue(
            summary["q27_real_source_compatible_preload_verified_synthetically"]
        )
        self.assertEqual(summary["q27_injected_guard_stale_events"], 1)
        self.assertTrue(summary["both_books_scrubbed"])
        self.assertTrue(summary["partial_resnapshot_hidden"])
        self.assertTrue(summary["prior_sse_resync_required"])
        self.assertTrue(summary["new_dual_snapshots_confirmed"])
        self.assertFalse(summary["five_hour_certified"])
        public = os.environ.get("Q27_PUBLIC_SYNTHETIC_RECEIPTS")
        if public:
            path = Path(public) / "q27_heartbeat_only_recovery_synthetic.json"
            with path.open("x") as stream:
                json.dump({
                    "schema": "q27_live_compatible_guard_synthetic_test_v1",
                    "verdict": "PASS_SYNTHETIC_ONLY",
                    **summary,
                }, stream, indent=2, sort_keys=True)
                stream.write("\n")

    def test_no_initial_heartbeat_forces_new_dual_snapshot_session(self):
        with tempfile.TemporaryDirectory(prefix="q27_first_hb_") as tmp:
            work = Path(tmp)
            private = work / "q27_private.jsonl"
            tap = work / "tap.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tap))
            listener.listen(1)
            listener.settimeout(4)
            engine = SourceEpochIsolatedEngine()
            done = [False]
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": "22",
                "SOAK_RAW_BUDGET_BYTES": "1000000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "SOAK_CHUNK_LIMIT_BYTES": "8000000",
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
                "V36_TAP_FIXTURE_MODE": "1",
                "V36_LIVE_TAP_SOCKET": str(tap),
                "Q26_TEST_SCENARIO": "NO_INITIAL_HEARTBEAT",
                "Q27_HEARTBEAT_GUARD_ENABLED": "1",
                "Q27_HEARTBEAT_TRACE_PRIVATE": str(private),
            }
            proc = subprocess.Popen([
                shutil.which("node"), "--import",
                str(ROOT / "tests/q26_three_fault_coinbase_mock.mjs"),
                "--import", str(ROOT / "scripts/q27_heartbeat_guard.mjs"),
                str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
            ], cwd=work, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            reader = None
            try:
                try:
                    conn, _ = listener.accept()
                except TimeoutError as exc:
                    rc = proc.poll()
                    if rc is not None:
                        _, stderr = proc.communicate(timeout=3)
                        raise AssertionError(
                            "Q27_SYNTHETIC_STARTUP_EXIT_CODE="
                            + str(rc) + ":"
                            + stderr.decode(errors="replace")[-1200:]
                        ) from exc
                    raise
                reader = threading.Thread(
                    target=read_tap, args=(conn, engine, done), daemon=True
                )
                reader.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if engine.health()["connection_id"] == 0:
                        break
                    time.sleep(0.04)
                self.assertEqual(engine.health()["connection_id"], 0)
                # No first heartbeat: even both snapshots and acks must
                # never make source HEALTHY or expose a partial book.
                self.assertEqual(engine.health()["status"], "WAITING_FOR_SNAPSHOT")
                self.assertIsNone(engine.snapshot("BTC-USD")["state"])
                self.assertIsNone(engine.snapshot("ETH-USD")["state"])
                deadline = time.monotonic() + 14
                while time.monotonic() < deadline:
                    health = engine.health()
                    if (health["connection_id"] == 1
                            and health["status"] == "HEALTHY"):
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Q27 startup heartbeat timeout did not recover")
                self.assertEqual(health["connection_gaps"], 1)
                self.assertGreaterEqual(health["q25_revocations_total"], 1)
                self.assertNotEqual(
                    health["source_session_history"][0]["session_id"],
                    health["session_id"],
                )
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=7)
                done[0] = True
                if reader:
                    reader.join(timeout=4)
                listener.close()
            trace = read_trace(private)
            events = [v["event"] for v in trace]
            self.assertEqual(events.count("FIRST_HEARTBEAT_TIMEOUT"), 1)
            self.assertEqual(
                [v["socket_index"] for v in trace
                 if v["event"] == "SOCKET_CONSTRUCTED"], [0, 1],
            )
            evidence = build_receipt(private)
            self.assertEqual(evidence["interventions_total"], 1)
            self.assertEqual(evidence["first_heartbeat_timeout_events"], 1)
            self.assertFalse(evidence["real_orders_enabled"])
            archive = next(
                (work / "soak_evidence").glob("coinbase_btc_eth_soak_*")
            )
            m = json.loads((archive / "manifest.json").read_bytes())
            self.assertTrue(m["synthetic_fixture"])
            self.assertFalse(m["ready_for_research"])
            self.assertEqual(m["total_reconnects"], 1)
            public = os.environ.get("Q27_PUBLIC_SYNTHETIC_RECEIPTS")
            if public:
                with (
                    Path(public) / "q27_initial_heartbeat_timeout_synthetic.json"
                ).open("x") as fp:
                    json.dump({
                        "schema": "q27_initial_heartbeat_gap_synthetic_test_v1",
                        "verdict": "PASS_SYNTHETIC_ONLY",
                        "initial_dual_book_with_no_heartbeat_never_healthy": True,
                        "one_initial_heartbeat_timeout": True,
                        "new_epoch_dual_snapshot_passed": True,
                        "reconnects": 1,
                        "real_orders_enabled": False,
                        "five_hour_certified": False,
                    }, fp, sort_keys=True, indent=2)
                    fp.write("\n")


if __name__ == "__main__":
    unittest.main()
