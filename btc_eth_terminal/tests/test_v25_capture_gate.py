"""v25 gate negative controls; synthetic metadata cannot prove real market truth."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    from v25_capture_admission import (
        V25AdmissionError,
        assess_archive,
        evaluate_operator_observation,
        load_json,
        parse_utc,
    )
    return V25AdmissionError, assess_archive, evaluate_operator_observation, load_json, parse_utc

V25AdmissionError, assess_archive, evaluate_operator_observation, load_json, parse_utc = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

SCRIPT_HASH = sha256(
    (ROOT / "scripts" / "btc_eth_multiday_v20.mjs").read_bytes()
).hexdigest()
MANIFEST_SHA = "e" * 64


def utc(delta=0):
    return (
        (datetime(2026, 11, 14, tzinfo=UTC) + timedelta(seconds=delta))
        .isoformat()
        .replace("+00:00", "Z")
    )


def manifest(duration=60):
    return {
        "required_duration_seconds": duration,
        "sampled_from_utc": utc(0),
        "finished_at_utc": utc(duration + 0.2),
        "status": "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "ready_for_research": True,
        "producer_release": "v20_multiday_bounded",
        "synthetic_fixture": False,
        "controlled_fault_injection": None,
        "connections": [{"id": 0}],
        "faults": [],
    }


def observation(duration=60):
    clock = {
        "method": "chronyc_tracking_host_reported",
        "command_exit_code": 0,
        "leap_status": "Normal",
        "stratum": 2,
        "system_offset_seconds": 0.001,
    }
    return {
        "schema": "v25_operator_evidence_v1",
        "run_mode": "real_public_24h" if duration == 86400 else "real_public_smoke_60s",
        "archive_manifest_sha256": MANIFEST_SHA,
        "capture_exit_code": 0,
        "injection_options_disabled": True,
        "producer_script_sha256": SCRIPT_HASH,
        "before": {
            "boot_id_digest": "a" * 64,
            "storage_root": "/fixed/path",
            "dns_status": "RESOLVED_NO_WEBSOCKET_ATTESTATION",
            "observed_utc": utc(-0.5),
            "monotonic_ns": 10_000_000_000,
            "clock": clock,
        },
        "after": {
            "boot_id_digest": "a" * 64,
            "storage_root": "/fixed/path",
            "dns_status": "FAILED_DNS_LOOKUP",
            "observed_utc": utc(duration + 1),
            "monotonic_ns": 10_000_000_000 + int((duration + 1.5) * 1e9),
            "clock": clock,
        },
    }


class GateEvidenceTests(TestCase):
    def test_valid_observer_cannot_prove_market_truth(self):
        self.assertEqual(
            evaluate_operator_observation(observation(), manifest(), MANIFEST_SHA), []
        )

    def test_valid_24h_observer_metadata(self):
        self.assertEqual(
            evaluate_operator_observation(
                observation(86400), manifest(86400), MANIFEST_SHA
            ),
            [],
        )

    def test_bad_manifest_binding(self):
        ob = observation()
        ob["archive_manifest_sha256"] = "f" * 64
        self.assertIn(
            "MANIFEST_BINDING_HASH_MISMATCH",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_test_fixture_flag(self):
        ob = observation()
        ob["injection_options_disabled"] = False
        self.assertIn(
            "SYNTHETIC_INJECTION_NOT_PROVEN_DISABLED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_bad_script_digest(self):
        ob = observation()
        ob["producer_script_sha256"] = "f" * 64
        self.assertIn(
            "PRODUCER_SCRIPT_HASH_MISMATCH",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_error_exit(self):
        ob = observation()
        ob["capture_exit_code"] = 2
        self.assertIn(
            "CAPTURE_EXIT_NONZERO",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_wrong_duration_mode(self):
        ob = observation()
        ob["run_mode"] = "real_public_24h"
        self.assertIn(
            "FULL_DAY_DURATION_MISMATCH",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_boot_change(self):
        ob = observation()
        ob["after"]["boot_id_digest"] = "b" * 64
        self.assertIn(
            "BOOT_ID_MISMATCH_OR_UNKNOWN",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_storage_migration(self):
        ob = observation()
        ob["after"]["storage_root"] = "/different"
        self.assertIn(
            "STORAGE_ROOT_CHANGED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_no_dns(self):
        ob = observation()
        ob["before"]["dns_status"] = "FAILED_DNS_LOOKUP"
        self.assertIn(
            "INITIAL_DNS_NOT_VERIFIED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_wall_clock_jump(self):
        ob = observation()
        ob["after"]["observed_utc"] = utc(70)
        self.assertIn(
            "HOST_CLOCK_JUMP_OVER_2S",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_short_monotonic_window(self):
        ob = observation()
        ob["after"]["monotonic_ns"] = 11_000_000_000
        self.assertIn(
            "MONOTONIC_CAPTURE_UNDERRUN",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_window_does_not_bracket_capture(self):
        ob = observation()
        ob["before"]["observed_utc"] = utc(2)
        self.assertIn(
            "HOST_OBSERVATIONS_DO_NOT_BRACKET_SOURCE",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_missing_clock(self):
        ob = observation()
        ob["after"]["clock"] = {"method": "unavailable"}
        self.assertIn(
            "HOST_REPORTED_CHRONY_UNVERIFIED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_clock_stratum_bool_not_int(self):
        ob = observation()
        ob["after"]["clock"]["stratum"] = True
        self.assertIn(
            "HOST_REPORTED_CHRONY_UNVERIFIED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_clock_offset_too_large(self):
        ob = observation()
        ob["before"]["clock"]["system_offset_seconds"] = 0.2
        self.assertIn(
            "HOST_REPORTED_CHRONY_UNVERIFIED",
            evaluate_operator_observation(ob, manifest(), MANIFEST_SHA),
        )

    def test_malformed_utc(self):
        with self.assertRaises(V25AdmissionError):
            parse_utc("2026-11-14T00:00:00-05:00")

    def test_duplicate_json_keys_refused(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text('{"x":1,"x":2}')
            with self.assertRaises(V25AdmissionError):
                load_json(p)

    def test_symlink_observation_refused(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text('{"a":1}')
            q = Path(d) / "other.json"
            q.symlink_to(p)
            with self.assertRaises(V25AdmissionError):
                load_json(q)

    def test_missing_obs_rejected(self):
        self.assertIn(
            "MISSING_OR_INCOMPATIBLE_OPERATOR_EVIDENCE",
            evaluate_operator_observation(None, manifest(), MANIFEST_SHA),
        )


class GateReplayTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "manifest.json").write_text(json.dumps(manifest()))
        self.primary = {
            "message_count": 10,
            "chunk_count": 1,
            "status": "INTERNALLY_REPLAYED_NO_KNOWN_GAP",
            "books_final": {"BTC-USD": {"sha256": "a"}, "ETH-USD": {"sha256": "b"}},
        }
        self.secondary = {
            "messages": 10,
            "chunks": 1,
            "books_final": {"BTC-USD": {"sha256": "a"}, "ETH-USD": {"sha256": "b"}},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_gate(self, obs=None):
        with (
            patch("v25_capture_admission.primary_replay", return_value=self.primary),
            patch(
                "v25_capture_admission.secondary_replay", return_value=self.secondary
            ),
        ):
            return assess_archive(self.root, obs)

    def test_no_operator_never_promotes(self):
        out = self.run_gate()
        self.assertEqual(out["status"], "INTERNAL_REPLAY_ONLY_HOST_EVIDENCE_INCOMPLETE")
        self.assertFalse(out["candidate_real_60s_smoke"])

    def test_observer_can_reach_internal_smoke_candidate_only(self):
        ob = observation()
        ob["archive_manifest_sha256"] = sha256(
            (self.root / "manifest.json").read_bytes()
        ).hexdigest()
        f = self.root / "evidence.json"
        f.write_text(json.dumps(ob))
        out = self.run_gate(f)
        self.assertTrue(out["candidate_real_60s_smoke"])
        self.assertFalse(out["independent_exchange_truth_verified"])
        self.assertFalse(out["executable_alpha_verified"])

    def test_operator_hash_mismatch_never_promotes(self):
        f = self.root / "evidence.json"
        f.write_text(json.dumps(observation()))
        self.assertFalse(self.run_gate(f)["candidate_real_60s_smoke"])

    def test_synthetic_is_source_rejected(self):
        m = manifest()
        m["synthetic_fixture"] = True
        (self.root / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(V25AdmissionError, "ARCHIVE_NOT_SINGLE_CONNECTION"):
            self.run_gate()

    def test_replay_rejected(self):
        self.primary["status"] = "ARCHIVED_BUT_NOT_ADMITTED"
        with self.assertRaisesRegex(V25AdmissionError, "SOURCE_NOT_ACCEPTED"):
            self.run_gate()

    def test_independent_book_disagreement(self):
        self.secondary["books_final"]["ETH-USD"]["sha256"] = "x"
        with self.assertRaisesRegex(V25AdmissionError, "INDEPENDENT_BOOK"):
            self.run_gate()

    def test_independent_count_disagreement(self):
        self.secondary["messages"] = 11
        with self.assertRaisesRegex(V25AdmissionError, "INDEPENDENT_COUNT"):
            self.run_gate()

    def test_legacy_does_not_gain_v25_status(self):
        m = manifest()
        m["producer_release"] = "v13_rolling_bounded"
        (self.root / "manifest.json").write_text(json.dumps(m))
        self.assertIn(
            "LEGACY_PRODUCER_NOT_V25_RUN_ELIGIBLE",
            self.run_gate()["operator_evidence_failures"],
        )

    def test_multiple_connections_rejected(self):
        m = manifest()
        m["connections"] = [{"id": 0}, {"id": 1}]
        (self.root / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(V25AdmissionError, "ARCHIVE_NOT_SINGLE_CONNECTION"):
            self.run_gate()

    def test_manifest_mutated_during_replay(self):
        def mutate(root):
            (root / "manifest.json").write_text(
                json.dumps({**manifest(), "new": "value"})
            )
            return self.primary

        with (
            patch("v25_capture_admission.primary_replay", side_effect=mutate),
            patch(
                "v25_capture_admission.secondary_replay", return_value=self.secondary
            ),
        ):
            with self.assertRaisesRegex(V25AdmissionError, "MANIFEST_MUTATED"):
                assess_archive(self.root)
