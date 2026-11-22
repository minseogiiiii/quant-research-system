"""Q22 pure adversarial contracts. These are NOT actual five-hour evidence."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from q22_ci_5h_supervisor import (  # noqa: E402
    DISK_RESERVE,
    Q22Rejected,
    check_private_health,
    evaluate_monitoring,
    evaluate_window,
    projected_archive_bound,
)
from q22_ci_full_replay import (  # noqa: E402
    FiveHourReplayRejected,
    validate_observer_evidence,
)
from q22_ci_long_observer import (  # noqa: E402
    ARCHIVE_BUDGET,
    CI_DURATION,
    PREFLIGHT_MARGIN,
    RAW_BUDGET,
    SMOKE_DURATION,
    ci_source_policy,
)


def good_health():
    return {
        "mode": "LIVE_SOURCE",
        "live_feed": True,
        "status": "HEALTHY",
        "operationally_healthy": True,
        "trading_enabled": False,
        "snapshots": ["BTC-USD", "ETH-USD"],
        "subscriptions": ["level2", "market_trades", "heartbeats"],
        "connection_gaps": 0,
        "connection_id": 0,
        "session_id": "f" * 32,
        "messages": 100,
        "last_sequence": 99,
        "heartbeats": 10,
        "last_receiver_time_ms": 1_800_000_000_000,
    }


def good_metrics():
    return {
        "max_sse_clients": 8,
        "monitor_transport": "UNIX_SOCKET",
        "trading_enabled": False,
        "execution_authorized": False,
        "active_sse_connections": 8,
    }


def good_fanout(seconds=240):
    return {
        "schema": "v36_3_genuine_public_live_fanout_v1",
        "status": "PASS",
        "mode": "REAL_PUBLIC_COINBASE_LIVE_NOT_ARCHIVE",
        "trading_enabled": False,
        "certified_24h": False,
        "max_active_sse": 8,
        "elapsed_seconds": seconds,
        "health_latency_p95_ms": 2,
        "clients": [
            {
                "client": i,
                "product": ("BTC-USD", "ETH-USD")[i % 2],
                "snapshots": 1,
                "states": 30,
                "resync": 0,
                "errors": [],
            }
            for i in range(8)
        ],
    }


def good_sample(i=0):
    return {
        "index": i,
        "monotonic_ns": 1_000_000_000 + 15_000_000_000 * i,
        "source": {
            "session_hash": "a" * 64,
            "messages": 100 + i,
            "heartbeats": 10 + i,
            "staleness_ms": 100,
        },
        "observer_pid": 123,
        "collector_pid": 456,
        "rss_total_bytes": 30_000_000,
        "fd_total": 25,
        "cpu_ticks_total": i * 10,
        "disk_free_bytes": DISK_RESERVE + 999_000_000,
        "trading_enabled": False,
    }


def good_evidence(source):
    return {
        "schema": "q22_ci_observer_evidence_v1",
        "mode": "LIVE_SOURCE",
        "ci_ephemeral_runner_only": True,
        "durable_operator_mode": False,
        "execution_authorized": False,
        "trading_enabled": False,
        "collector_exit_code": 0,
        "capture_ready_for_research": True,
        "observer_reader_finished": True,
        "observer_source_complete": True,
        "capture_fault_count": 0,
        "elapsed_seconds": 18005,
        "observer_health_before_stop": {
            "mode": "LIVE_SOURCE",
            "trading_enabled": False,
            "connection_gaps": 0,
            "connection_id": 0,
            "snapshots": ["BTC-USD", "ETH-USD"],
        },
        "captured_manifest_sha256": "a" * 64,
        "captured_dir": str(source),
    }


class Q22CiPolicy(unittest.TestCase):
    def test_exact_duration_limits_and_host_free_space(self):
        enough = ARCHIVE_BUDGET + DISK_RESERVE + PREFLIGHT_MARGIN
        self.assertTrue(ci_source_policy(
            CI_DURATION, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, enough))
        self.assertTrue(ci_source_policy(
            SMOKE_DURATION, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, enough))
        self.assertFalse(ci_source_policy(
            3600, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, enough))
        self.assertFalse(ci_source_policy(
            CI_DURATION, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, enough - 1))

    def test_never_reduce_existing_budget_or_allow_boolean(self):
        enough = ARCHIVE_BUDGET + DISK_RESERVE + PREFLIGHT_MARGIN
        for options in (
            (CI_DURATION, RAW_BUDGET - 1, ARCHIVE_BUDGET, DISK_RESERVE, enough),
            (CI_DURATION, RAW_BUDGET, ARCHIVE_BUDGET - 1, DISK_RESERVE, enough),
            (CI_DURATION, RAW_BUDGET, ARCHIVE_BUDGET, 0, enough),
            (True, RAW_BUDGET, ARCHIVE_BUDGET, DISK_RESERVE, enough),
        ):
            with self.subTest(options=options):
                self.assertFalse(ci_source_policy(*options))

    def test_projected_archive_normal_rate(self):
        result = projected_archive_bound(
            300, 21_000_000, 14_000_000_000, CI_DURATION)
        self.assertEqual(result["projected_archived_bytes_2p5x"], 3_150_000_000)

    def test_projected_archive_excessive_volume(self):
        with self.assertRaises(Q22Rejected):
            projected_archive_bound(
                300, 80_000_000, 14_000_000_000, CI_DURATION)

    def test_projection_rejects_disk_exhaustion(self):
        with self.assertRaises(Q22Rejected):
            projected_archive_bound(
                300, 21_000_000, DISK_RESERVE + 800_000_000, CI_DURATION)


class Q22SourceContinuity(unittest.TestCase):
    def test_valid_private_health_contract(self):
        from unittest.mock import patch

        with patch("q22_ci_5h_supervisor.time.time_ns",
                   return_value=1_800_000_000_500_000_000):
            row = check_private_health(good_health(), good_metrics(), None)
            nxt = good_health()
            nxt["messages"] += 10
            nxt["last_sequence"] += 10
            row2 = check_private_health(nxt, good_metrics(), row)
        self.assertEqual(row2["messages"], 110)
        self.assertEqual(row2["session_hash"], row["session_hash"])

    def test_reject_gap_session_change_stale_or_order_mode(self):
        from unittest.mock import patch

        for key, bad in (
            ("connection_gaps", 1),
            ("session_id", "e" * 32),
            ("trading_enabled", True),
            ("messages", 0),
            ("last_sequence", 0),
            ("status", "STALE"),
            ("last_receiver_time_ms", 1_700_000_000_000),
        ):
            h = good_health()
            h[key] = bad
            previous = {"session_hash": "f" * 64, "messages": 99,
                        "heartbeats": 9}
            with self.subTest(key=key), patch(
                "q22_ci_5h_supervisor.time.time_ns",
                return_value=1_800_000_000_500_000_000
            ), self.assertRaises(Q22Rejected):
                check_private_health(h, good_metrics(), previous)

    def test_reject_bad_operator_cap_or_private_status(self):
        from unittest.mock import patch

        for key, bad in (
            ("max_sse_clients", 7),
            ("active_sse_connections", 9),
            ("execution_authorized", True),
        ):
            m = good_metrics()
            m[key] = bad
            with self.subTest(key=key), patch(
                "q22_ci_5h_supervisor.time.time_ns",
                return_value=1_800_000_000_500_000_000
            ), self.assertRaises(Q22Rejected):
                check_private_health(good_health(), m, None)

    def test_live_eight_client_report_admission(self):
        import json

        content = "PASS_REAL_PUBLIC_LIVE_FANOUT " + json.dumps(good_fanout())
        valid = evaluate_window(content, 240)
        self.assertEqual(valid["max_active_sse"], 8)
        self.assertEqual(valid["total_states"], 240)

    def test_never_accept_missing_one_client_or_replayed_mode(self):
        import json

        for key, bad in (
            ("max_active_sse", 7),
            ("mode", "ARCHIVE_SIMULATION_NOT_LIVE"),
            ("trading_enabled", True),
        ):
            r = good_fanout()
            r[key] = bad
            with self.subTest(key=key), self.assertRaises(Q22Rejected):
                evaluate_window(
                    "PASS_REAL_PUBLIC_LIVE_FANOUT " + json.dumps(r), 240)

    def test_reject_sse_state_duplicate_or_client_error(self):
        import json

        r = good_fanout()
        r["clients"][2]["states"] = 0
        with self.assertRaises(Q22Rejected):
            evaluate_window(
                "PASS_REAL_PUBLIC_LIVE_FANOUT " + json.dumps(r), 240)

    def test_monitor_enforces_continuity_and_no_restart(self):
        rows = [good_sample(i) for i in range(19)]
        valid = evaluate_monitoring(rows, SMOKE_DURATION,
                                    [{"total_states": 80}], 1)
        self.assertEqual(valid["max_open_fds"], 25)
        self.assertGreaterEqual(valid["cpu_cores_peak"], 0)
        self.assertEqual(valid["rss_growth_bytes"], 0)
        self.assertTrue(valid["collector_pid_stable"])
        bad = [dict(v) for v in rows]
        bad[4]["collector_pid"] = 1234
        with self.assertRaises(Q22Rejected):
            evaluate_monitoring(bad, SMOKE_DURATION,
                                [{"total_states": 80}], 1)

    def test_monitor_rejects_decreasing_cpu_ticks(self):
        rows = [good_sample(i) for i in range(19)]
        rows[8]["cpu_ticks_total"] = 0
        with self.assertRaises(Q22Rejected):
            evaluate_monitoring(rows, SMOKE_DURATION,
                                [{"total_states": 80}], 1)

    def test_monitor_detects_unobserved_gap(self):
        rows = [good_sample(i) for i in range(19)]
        rows[6]["monotonic_ns"] += 60_000_000_000
        with self.assertRaises(Q22Rejected):
            evaluate_monitoring(rows, SMOKE_DURATION,
                                [{"total_states": 80}], 1)


class Q22IndependentAuditContracts(unittest.TestCase):
    def test_source_evidence_scope_and_external_pin_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "private"
            source = work / "soak_evidence" / "coinbase_btc_eth_soak_x"
            source.mkdir(parents=True)
            evidence = good_evidence(source)
            actual, digest = validate_observer_evidence(work, evidence)
            self.assertEqual(actual, source)
            self.assertEqual(digest, "a" * 64)

    def test_reject_incomplete_capture_or_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "private"
            source = work / "soak_evidence" / "coinbase_btc_eth_soak_x"
            source.mkdir(parents=True)
            for key, value in (
                ("observer_source_complete", False),
                ("capture_fault_count", 1),
                ("collector_exit_code", 1),
                ("trading_enabled", True),
                ("elapsed_seconds", 17400),
                ("captured_manifest_sha256", "0"),
            ):
                evidence = good_evidence(source)
                evidence[key] = value
                with self.subTest(key=key), self.assertRaises(
                    FiveHourReplayRejected
                ):
                    validate_observer_evidence(work, evidence)

    def test_reject_source_from_another_private_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "private"
            other = Path(tmp) / "forged"
            other.mkdir()
            with self.assertRaises(FiveHourReplayRejected):
                validate_observer_evidence(work, good_evidence(other))


if __name__ == "__main__":
    unittest.main()
