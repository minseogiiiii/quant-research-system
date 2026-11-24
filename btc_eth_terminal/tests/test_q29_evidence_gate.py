"""Synthetic receipt contract tests; not actual market evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from q29_evidence_gate import (
    FILES,
    MANIFEST,
    EvidenceRejected,
    seal,
    verify,
)

SHA = "a" * 40
HASH = "b" * 64


class EvidenceGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.duration = 1800
        self.populate()

    def write(self, name, obj):
        (self.root / name).write_text(json.dumps(obj) + "\n")

    def populate(self):
        self.docs = {
            "preflight.json": {
                "source_mode": "LIVE_SOURCE", "trading_enabled": False,
            },
            "live_monitoring_summary.json": {
                "status": "PASS", "capture_target_seconds": 1800,
                "windows_passed": 3, "source_session_stable": True,
                "collector_pid_stable": True, "q25_epoch_guard_verified": True,
                "q27_30m_guarded_only": True, "ephemeral_runner_only": True,
                "real_orders_enabled": False, "order_execution_enabled": False,
                "24h_persistent_host_certified": False,
                "sample_span_seconds": 1785, "max_rss_bytes": 188_000_000,
                "rss_growth_bytes": 20_000_000, "max_open_fds": 43,
                "min_disk_free_bytes": 90_000_000_000,
                "sse_total_states": 300, "total_source_messages_last_sample": 990,
                "sse_windows": [
                    {"status": "PASS", "window": i, "max_active_sse": 8,
                     "elapsed_seconds": 120, "total_states": 100}
                    for i in range(1, 4)
                ],
            },
            "replay.json": {
                "status": "PASS", "expected_source_seconds": 1800,
                "mode": "REAL_COINBASE_PUBLIC_SOURCE_CAPTURE",
                "source_connections": 1, "source_reconnects": 0,
                "primary_independent_final_books_agree": True,
                "real_orders_enabled": False,
                "persistent_host_24h_certified": False,
                "oos_profitability_verified": False, "source_monotonic_seconds": 1805,
                "messages": 1000, "chunks": 20, "archive_bytes": 172_000_000,
                "independent_final_book_sha256": {"BTC-USD": HASH, "ETH-USD": HASH},
                "manifest_output_sha256": HASH, "observer_evidence_sha256": HASH,
                "primary_final_state_sha256": HASH,
            },
            "q24_redacted_ws_close_receipt.json": {
                "trace_schema_verified": True, "opened_connections": 1,
                "observed_connections": 1, "websocket_error_events": 0,
                "close_events": 1, "probe_exited": True,
                "last_close_local_request": True, "raw_websocket_payloads_saved": False,
                "error_messages_or_close_reasons_saved": False,
                "websocket_endpoint_or_ip_saved": False,
                "root_network_cause_proven": False, "real_orders_enabled": False,
                "execution_authorized": False, "last_reported_heartbeat_count": 180,
                "last_reported_message_count": 1000, "events_observed": 36,
            },
            "q27_redacted_heartbeat_guard.json": {
                "trace_schema_verified": True, "synthetic_fixture": False,
                "opened_sockets_constructed": 1, "sockets_with_first_heartbeat": 1,
                "interventions_total": 0, "heartbeat_timeout_events": 0,
                "close_failure_events": 0, "collector_stop_observed": True,
                "real_orders_enabled": False, "execution_authorized": False,
                "last_heartbeat_count": 180, "last_nonheartbeat_message_count": 820,
                "observed_event_count": 6,
            },
            "q28_redacted_incident_receipt.json": {
                "classification": "DIAGNOSTIC_ONLY_NOT_SOURCE_ACCEPTANCE",
                "observed_termination_class": "LOCAL_NON_GUARD_CLOSE_OBSERVED",
                "q24_trace_validation": "COMMITTED_PREFIX_SCHEMA_VALID",
                "q27_trace_validation": "COMMITTED_PREFIX_SCHEMA_VALID",
                "q24_process_exit_observed": True, "q27_process_exit_observed": True,
                "q24_trace_complete_for_normal_acceptance": True,
                "q27_trace_complete_for_normal_acceptance": True,
                "known_supervisor_failfast": False, "guard_interventions_observed": 0,
                "network_root_cause_proven": False, "real_orders_enabled": False,
                "source_continuity_certified": False,
                "raw_payload_or_close_reason_published": False,
                "first_socket": {
                    "first_socket_close_observed": True,
                    "first_socket_local_close_request_observed": True,
                    "first_socket_error_events": 0,
                    "first_socket_q27_timeout_events": 0,
                    "second_socket_constructed": False,
                },
                "q24_committed_events": 36, "q27_committed_events": 6,
            },
        }
        for name, obj in self.docs.items():
            obj["schema"] = FILES[name]
            self.write(name, obj)

    def seal(self):
        return seal(self.root, self.duration, SHA, 42, 1)

    def verify(self, digest, *, run=42, sha=SHA):
        return verify(self.root, self.duration, sha, run, 1, digest)

    def test_roundtrip(self):
        d = self.seal()
        self.assertEqual(self.verify(d), d)

    def test_missing_and_extra(self):
        (self.root / "preflight.json").unlink()
        with self.assertRaises(EvidenceRejected):
            self.seal()
        self.write("preflight.json", self.docs["preflight.json"])
        (self.root / "raw.jsonl").write_text("PRIVATE")
        with self.assertRaises(EvidenceRejected):
            self.seal()

    def test_same_semantics_changed_bytes(self):
        d = self.seal()
        p = self.root / "preflight.json"
        p.write_bytes(p.read_bytes() + b" ")
        with self.assertRaises(EvidenceRejected):
            self.verify(d)

    def test_manifest_substitution_and_wrong_identity(self):
        d = self.seal()
        for run, sha in [(43, SHA), (42, "c" * 40)]:
            with self.assertRaises(EvidenceRejected):
                self.verify(d, run=run, sha=sha)
        with self.assertRaises(EvidenceRejected):
            self.verify("d" * 64)
        (self.root / MANIFEST).unlink()
        with self.assertRaises(EvidenceRejected):
            self.verify(d)

    def test_no_rerun_reuses_manifest(self):
        with self.assertRaises(EvidenceRejected):
            seal(self.root, self.duration, SHA, 42, 2)

    def test_duplicate_and_nonfinite(self):
        p = self.root / "preflight.json"
        p.write_text('{"schema":"x","schema":"y"}')
        with self.assertRaises(EvidenceRejected):
            self.seal()
        p.write_text('{"x":NaN}')
        with self.assertRaises(EvidenceRejected):
            self.seal()

    def test_symlink(self):
        p = self.root / "preflight.json"
        p.unlink()
        p.symlink_to("replay.json")
        with self.assertRaises(EvidenceRejected):
            self.seal()

    def test_invalid_acceptance_witnesses(self):
        cases = [
            ("replay.json", "source_reconnects", 1),
            ("replay.json", "source_monotonic_seconds", 1794),
            ("replay.json", "primary_independent_final_books_agree", False),
            ("replay.json", "real_orders_enabled", True),
            ("replay.json", "messages", 999),
            ("live_monitoring_summary.json", "windows_passed", 2),
            ("live_monitoring_summary.json", "max_rss_bytes", 3 * 1024**3),
            ("live_monitoring_summary.json", "sample_span_seconds", 1600),
            ("q24_redacted_ws_close_receipt.json", "probe_exited", False),
            ("q27_redacted_heartbeat_guard.json", "synthetic_fixture", True),
            ("q27_redacted_heartbeat_guard.json", "interventions_total", 1),
            ("q28_redacted_incident_receipt.json", "known_supervisor_failfast", True),
            ("q28_redacted_incident_receipt.json", "q24_process_exit_observed", False),
        ]
        for name, field, value in cases:
            with self.subTest(name=name, field=field):
                original = self.docs[name]
                self.write(name, {**original, field: value})
                with self.assertRaises(EvidenceRejected):
                    self.seal()
                self.write(name, original)


if __name__ == "__main__":
    unittest.main()
