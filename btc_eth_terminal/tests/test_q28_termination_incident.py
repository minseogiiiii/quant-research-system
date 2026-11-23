"""Q28 crash-safe close-cause classification; no synthetic fixture is LIVE.

Tests prove strict normal receipts still reject absent PROCESS_EXIT or a
truncated last line, while Q28 may preserve ONLY committed validated events.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from q24_ws_diagnostic_receipt import Q24TraceRejected, read_private_trace
from q27_heartbeat_receipt import Q27TraceRejected, read_trace
from q28_transport_incident_receipt import incident_receipt, publish_incident


def q24_event(n: int, event: str, *, socket: int | None = 0,
              code: int | None = None, local: bool = False) -> dict:
    return {
        "schema": "q24_ws_close_event_v1", "seq": n, "event": event,
        "connection_index": socket, "elapsed_ms": n * 100,
        "connection_uptime_ms": n * 100 if socket is not None else None,
        "message_count": 8 if socket is not None else 0,
        "heartbeat_count": 2 if socket is not None else 0,
        "last_message_age_ms": 10 if socket is not None else None,
        "last_heartbeat_age_ms": 4600 if socket is not None else None,
        "close_code": code if event == "SOCKET_CLOSE" else None,
        "was_clean": False if event == "SOCKET_CLOSE" else None,
        "local_close_requested": local,
        "reconnect_delay_ms": None,
        "ping_pong_control_frames_visible": False,
        "real_orders_enabled": False,
    }


def q27_event(n: int, event: str, *, socket: int | None = 0,
              interventions: int = 0) -> dict:
    return {
        "schema": "q27_heartbeat_guard_event_v1", "index": n,
        "event": event, "socket_index": socket, "elapsed_ms": n * 100,
        "socket_age_ms": n * 100 if socket is not None else None,
        "last_heartbeat_age_ms": 4700 if socket is not None else None,
        "heartbeat_count": 2 if socket is not None else 0,
        "other_message_count": 8 if socket is not None else 0,
        "timeout_threshold_ms": 4500, "startup_grace_ms": 9000,
        "interventions_total": interventions,
        "synthetic_fixture": True,
        "real_orders_enabled": False, "native_ping_pong_visible": False,
    }


class CrashEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="q28_incident_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "q24_private.jsonl"
        self.guard = self.root / "q27_private.jsonl"
        self.public = self.root / "redacted_receipt.json"

    @staticmethod
    def write(path: Path, rows: list[dict], tail: bytes = b"") -> None:
        path.write_bytes(
            b"".join(json.dumps(x, sort_keys=True).encode() + b"\n"
                     for x in rows) + tail
        )
        os.chmod(path, 0o600)

    def samples(self, *, guarded=True):
        ws = [
            q24_event(0, "TRACE_START", socket=None),
            q24_event(1, "SOCKET_CONSTRUCTED"),
            q24_event(2, "SOCKET_OPEN"),
        ]
        guard = [
            q27_event(0, "GUARD_START", socket=None),
            q27_event(1, "SOCKET_CONSTRUCTED"),
            q27_event(2, "SOCKET_OPEN"),
            q27_event(3, "FIRST_HEARTBEAT"),
        ]
        if guarded:
            guard.append(q27_event(
                4, "HEARTBEAT_STALE_FORCE_CLOSE", interventions=1
            ))
            ws.append(q24_event(3, "LOCAL_CLOSE_REQUEST", local=True))
        ws.append(q24_event(
            4 if guarded else 3, "SOCKET_CLOSE",
            code=1005 if guarded else 1006, local=guarded,
        ))
        return ws, guard

    def test_guard_intervention_without_process_exit_preserved_not_approved(self):
        ws, guard = self.samples()
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        with self.assertRaises(Q27TraceRejected):
            read_trace(self.guard)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(
            report["observed_termination_class"],
            "LOCAL_Q27_HEARTBEAT_GUARD_CLOSE_OBSERVED",
        )
        self.assertTrue(
            report["first_socket"]["first_socket_local_close_request_observed"]
        )
        self.assertEqual(report["first_socket"]["first_socket_close_code"], 1005)
        self.assertEqual(report["guard_interventions_observed"], 1)
        self.assertFalse(report["q27_process_exit_observed"])
        self.assertFalse(report["q27_trace_complete_for_normal_acceptance"])
        self.assertFalse(report["source_continuity_certified"])
        self.assertFalse(report["real_orders_enabled"])
        pub = publish_incident(self.ws, self.guard, self.public)
        self.assertEqual(pub, json.loads(self.public.read_text()))

    def test_abnormal_1006_with_no_local_request_is_not_attributed_to_coinbase(self):
        ws, guard = self.samples(guarded=False)
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(
            report["observed_termination_class"],
            "ABNORMAL_CLOSE_1006_WITHOUT_LOCAL_REQUEST",
        )
        self.assertFalse(report["network_root_cause_proven"])
        self.assertEqual(report["guard_interventions_observed"], 0)

    def test_socket_error_prior_to_unrequested_close_is_classified(self):
        ws, guard = self.samples(guarded=False)
        ws.insert(3, q24_event(3, "SOCKET_ERROR"))
        ws[-1]["seq"] = 4
        ws[-1]["elapsed_ms"] = 400
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(
            report["observed_termination_class"],
            "WEBSOCKET_ERROR_THEN_UNREQUESTED_CLOSE_OBSERVED",
        )

    def test_truncated_tail_dropped_only_for_crash_forensics(self):
        ws, guard = self.samples()
        self.write(self.ws, ws, b'{"raw_secret":"UNCOMMITTED_PARTIAL')
        self.write(self.guard, guard, b'{"partial_event":')
        with self.assertRaises(Q24TraceRejected):
            read_private_trace(self.ws)
        with self.assertRaises(Q27TraceRejected):
            read_trace(self.guard)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(report["q24_committed_events"], len(ws))
        self.assertEqual(report["q27_committed_events"], len(guard))
        self.assertEqual(report["guard_interventions_observed"], 1)
        self.assertNotIn("UNCOMMITTED_PARTIAL", json.dumps(report))
        self.assertFalse(report["source_continuity_certified"])

    def test_malicious_complete_event_rejected_without_leaking_secrets(self):
        ws, guard = self.samples()
        ws[-1]["source_url"] = "wss://secret-or-price=911"
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(report["q24_committed_events"], 0)
        self.assertEqual(
            report["q24_trace_validation"],
            "UNTRUSTED_OR_UNREDACTED_WS_TRACE_EVENT",
        )
        self.assertEqual(
            report["observed_termination_class"],
            "Q27_TIMEOUT_BUT_CLOSE_TRACE_UNVERIFIED",
        )
        self.assertNotIn("secret", json.dumps(report))
        self.assertFalse(report["network_root_cause_proven"])

    def test_insecure_symlink_and_hardlink_rejected(self):
        ws, guard = self.samples()
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        alias = self.root / "malicious-link.jsonl"
        alias.symlink_to(self.ws)
        report = incident_receipt(alias, self.guard)
        self.assertEqual(report["q24_committed_events"], 0)
        alias.unlink()
        alias = self.root / "hardlink.jsonl"
        os.link(self.ws, alias)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(report["q24_committed_events"], 0)
        alias.unlink()

    def test_missing_guard_file_cannot_prove_no_guard_intervention(self):
        ws, guard = self.samples(guarded=False)
        self.write(self.ws, ws)
        report = incident_receipt(self.ws, self.guard)
        self.assertEqual(report["q27_committed_events"], 0)
        self.assertIsNone(report["guard_interventions_observed"])
        self.assertEqual(
            report["observed_termination_class"],
            "ABNORMAL_CLOSE_1006_WITHOUT_LOCAL_REQUEST",
        )
        self.assertFalse(report["network_root_cause_proven"])

    def test_complete_normal_trace_is_still_classified_not_source_admitted(self):
        ws, guard = self.samples(guarded=False)
        ws.append(q24_event(4, "PROCESS_EXIT"))
        guard.append(q27_event(4, "PROCESS_EXIT"))
        self.write(self.ws, ws)
        self.write(self.guard, guard)
        report = incident_receipt(self.ws, self.guard, collector_failed=False)
        self.assertTrue(report["q24_process_exit_observed"])
        self.assertTrue(report["q27_process_exit_observed"])
        self.assertTrue(report["q24_trace_complete_for_normal_acceptance"])
        self.assertTrue(report["q27_trace_complete_for_normal_acceptance"])
        self.assertFalse(report["source_continuity_certified"])


if __name__ == "__main__":
    unittest.main()
