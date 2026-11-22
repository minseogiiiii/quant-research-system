"""Adversarial periodic host time evidence tests. Synthetic observations ONLY."""

from __future__ import annotations

import copy
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
from v25_capture_admission import evaluate_operator_observation
from v26_capture_crosscheck import CrosscheckError, crosscheck
from v26_clock_trace import ZERO, ClockTraceError, canonical, inspect_trace, record


def make_obs(i, period=20, wall_shift=0):
    t = datetime(2026, 11, 15, tzinfo=UTC) + timedelta(seconds=i * period + wall_shift)
    return {
        "observed_utc": t.isoformat().replace("+00:00", "Z"),
        "monotonic_ns": 1_000_000_000 + i * period * 1_000_000_000,
        "boot_id_digest": "a" * 64,
        "storage_root": "/srv/evidence-work",
        "free_storage_bytes": 5_000_000_000,
        "dns_status": "RESOLVED_NO_WEBSOCKET_ATTESTATION",
        "clock": {
            "method": "chronyc_tracking_host_reported",
            "command_exit_code": 0,
            "leap_status": "Normal",
            "stratum": 3,
            "system_offset_seconds": 0.005,
        },
    }


def write_trace(path, observations):
    prev = ZERO
    with path.open("wb") as stream:
        for i, obs in enumerate(observations):
            item = record(i, prev, obs)
            prev = item["hash"]
            stream.write(canonical(item) + b"\n")
    return path


def operator(obs, source=None):
    return {
        "schema": "v25_operator_evidence_v1",
        "run_mode": "real_public_smoke_60s",
        "before": obs[0],
        "after": obs[-1],
        "source_archive_dir": str(source) if source else None,
        "archive_manifest_sha256": "a" * 64,
        "capture_exit_code": 0,
        "capture_failure": None,
        "orders_enabled": False,
        "injection_options_disabled": True,
    }


class ClockTraceTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "trace.jsonl"
        self.obs = [make_obs(i) for i in range(4)]
        self.op = operator(self.obs)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, obs=None, op=None):
        write_trace(self.path, self.obs if obs is None else obs)
        return inspect_trace(
            self.path, self.op if op is None else op, mode="real_public_smoke_60s"
        )

    def test_normal_smoke_trace_passes(self):
        data = self.check()
        self.assertEqual(data["samples"], 4)
        self.assertEqual(data["max_monotonic_gap_seconds"], 20)
        self.assertFalse(data["independent_exchange_time_attested"])

    def test_middle_clock_jump_and_return_is_rejected(self):
        obs = copy.deepcopy(self.obs)
        obs[1]["observed_utc"] = make_obs(1, wall_shift=2)["observed_utc"]
        # v25 only sees matching before and after. v26 discovers the glitch.
        v25_legacy = {
            "required_duration_seconds": 60,
            "sampled_from_utc": make_obs(0)["observed_utc"],
            "finished_at_utc": make_obs(3)["observed_utc"],
        }
        v25_op = {
            **operator(self.obs),
            "run_mode": "real_public_smoke_60s",
            "producer_script_sha256": "bogus",
            "archive_manifest_sha256": "a" * 64,
        }
        self.assertNotIn(
            "HOST_CLOCK_JUMP_OVER_2S",
            evaluate_operator_observation(v25_op, v25_legacy, "a" * 64),
        )
        with self.assertRaisesRegex(ClockTraceError, "INTERMEDIATE_WALL_MONOTONIC"):
            self.check(obs)

    def test_missing_middle_sample_causes_gap(self):
        obs = [self.obs[0], self.obs[3], self.obs[3]]
        with self.assertRaisesRegex(ClockTraceError, "MISSING_INTERMEDIATE"):
            self.check(obs)

    def test_boot_change_rejected(self):
        obs = copy.deepcopy(self.obs)
        obs[2]["boot_id_digest"] = "b" * 64
        with self.assertRaisesRegex(ClockTraceError, "BOOT_OR_STORAGE"):
            self.check(obs)

    def test_storage_change_rejected(self):
        obs = copy.deepcopy(self.obs)
        obs[2]["storage_root"] = "/wrong"
        with self.assertRaisesRegex(ClockTraceError, "BOOT_OR_STORAGE"):
            self.check(obs)

    def test_chrony_unsync_midrun(self):
        obs = copy.deepcopy(self.obs)
        obs[1]["clock"]["leap_status"] = "Not synchronised"
        with self.assertRaisesRegex(ClockTraceError, "CHRONY_NOT_READY"):
            self.check(obs)

    def test_trace_bad_boot_length(self):
        obs = copy.deepcopy(self.obs)
        obs[1]["boot_id_digest"] = "abc"
        with self.assertRaisesRegex(ClockTraceError, "INVALID_BOOT"):
            self.check(obs)

    def test_monotonic_rollback_rejected(self):
        obs = copy.deepcopy(self.obs)
        obs[1]["monotonic_ns"] = obs[0]["monotonic_ns"]
        with self.assertRaisesRegex(ClockTraceError, "NONMONOTONIC"):
            self.check(obs)

    def test_missing_count_rejected(self):
        obs = [self.obs[0], self.obs[1]]
        op = operator(obs)
        with self.assertRaisesRegex(ClockTraceError, "INSUFFICIENT"):
            self.check(obs, op)

    def test_operator_endpoint_conflict_rejected(self):
        op = operator(self.obs)
        op["after"] = make_obs(7)
        with self.assertRaisesRegex(ClockTraceError, "ENDPOINT_MISMATCH"):
            self.check(op=op)

    def test_reordered_duplicate_sequence_rejected(self):
        self.check()
        items = self.path.read_bytes().splitlines()
        self.path.write_bytes(
            b"\n".join([items[0], items[2], items[1], items[3]]) + b"\n"
        )
        with self.assertRaisesRegex(ClockTraceError, "TRACE_SEQUENCE_GAP"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")

    def test_payload_mutation_rejected(self):
        self.check()
        lines = self.path.read_bytes().splitlines()
        entry = json.loads(lines[1])
        entry["observation"]["clock"]["stratum"] = 9
        lines[1] = canonical(entry)
        self.path.write_bytes(b"\n".join(lines) + b"\n")
        with self.assertRaisesRegex(ClockTraceError, "HASH_MISMATCH"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")

    def test_empty_trace_rejected(self):
        self.path.touch()
        with self.assertRaisesRegex(ClockTraceError, "MISSING"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")

    def test_symlink_trace_rejected(self):
        write_trace(Path(self.tmp.name) / "real", self.obs)
        self.path.symlink_to(Path(self.tmp.name) / "real")
        with self.assertRaisesRegex(ClockTraceError, "SYMLINK"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")

    def test_duplicate_json_key_rejected(self):
        self.check()
        with self.path.open("ab") as f:
            f.write(b'{"a":0,"a":1}\n')
        with self.assertRaisesRegex(ClockTraceError, "AMBIGUOUS"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")

    def test_24h_289_samples_passes(self):
        obs = [make_obs(i, 300) for i in range(289)]
        op = operator(obs)
        op["run_mode"] = "real_public_24h"
        write_trace(self.path, obs)
        data = inspect_trace(self.path, op, mode="real_public_24h")
        self.assertEqual(data["samples"], 289)
        self.assertEqual(data["max_monotonic_gap_seconds"], 300)

    def test_24h_sparse_trace_rejected(self):
        obs = [make_obs(i, 1200) for i in range(73)]
        op = operator(obs)
        op["run_mode"] = "real_public_24h"
        write_trace(self.path, obs)
        with self.assertRaisesRegex(ClockTraceError, "MISSING_INTERMEDIATE"):
            inspect_trace(self.path, op, mode="real_public_24h")

    def test_bool_monotonic_rejected(self):
        obs = copy.deepcopy(self.obs)
        obs[1]["monotonic_ns"] = True
        with self.assertRaisesRegex(ClockTraceError, "INVALID_MONOTONIC"):
            self.check(obs)

    def test_trace_oversized_rejected(self):
        self.path.write_bytes(b"x" * (600 * 24_000 + 1))
        with self.assertRaisesRegex(ClockTraceError, "TOO_LARGE"):
            inspect_trace(self.path, self.op, mode="real_public_smoke_60s")


class CrosscheckTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "manifest.json").write_text("{}")
        self.evidence = root / "evidence"
        self.evidence.mkdir()
        self.trace = self.evidence / "V26_CLOCK_TRACE.jsonl"
        self.obs = [make_obs(i) for i in range(4)]
        write_trace(self.trace, self.obs)
        self.op = operator(self.obs, self.source.resolve())
        self.operator_path = self.evidence / "V25_OPERATOR_EVIDENCE.json"
        self.context_path = self.evidence / "V26_TRACE_CONTEXT.json"
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        self.operator_path.write_text(json.dumps(self.op))
        context = {
            "schema": "v26_clock_trace_context_v1",
            "source_archive_dir": str(self.source.resolve()),
            "run_mode": self.op["run_mode"],
            "archive_manifest_sha256": self.op["archive_manifest_sha256"],
            "operator_evidence_sha256": sha256(
                self.operator_path.read_bytes()
            ).hexdigest(),
            "clock_trace_sha256": sha256(self.trace.read_bytes()).hexdigest(),
            "capture_failure": None,
            "trace_samples": len(self.obs),
            "disk_reserve_bytes": 4_000_000_000,
        }
        self.context_path.write_text(json.dumps(context))

    def base(self, status="HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE"):
        return {"status": status, "source_manifest_sha256": "a" * 64}

    def test_candidate_requires_both_gates(self):
        with patch("v26_capture_crosscheck.assess_archive", return_value=self.base()):
            data = crosscheck(self.source, self.evidence)
        self.assertTrue(data["candidate_host_monitored_source"])
        self.assertFalse(data["real_24h_verified_by_this_environment"])

    def test_prior_v25_gate_incomplete_blocks(self):
        with patch(
            "v26_capture_crosscheck.assess_archive",
            return_value=self.base("INTERNAL_REPLAY_ONLY_HOST_EVIDENCE_INCOMPLETE"),
        ):
            data = crosscheck(self.source, self.evidence)
        self.assertFalse(data["candidate_host_monitored_source"])

    def test_unbound_source_archive_rejected(self):
        self.op["source_archive_dir"] = "/different"
        self.write()
        with self.assertRaisesRegex(CrosscheckError, "V25_SOURCE_DIRECTORY"):
            crosscheck(self.source, self.evidence)

    def test_context_hash_mutation_rejected(self):
        self.write()
        modified = copy.deepcopy(self.op)
        modified["capture_exit_code"] = 12
        self.operator_path.write_text(json.dumps(modified))
        with self.assertRaisesRegex(CrosscheckError, "OPERATOR_EVIDENCE_HASH"):
            crosscheck(self.source, self.evidence)

    def test_trace_file_changed_rejected(self):
        self.trace.write_bytes(self.trace.read_bytes() + b"\n")
        with self.assertRaisesRegex(CrosscheckError, "CLOCK_TRACE_BYTES"):
            crosscheck(self.source, self.evidence)

    def test_operator_capture_failure_rejected(self):
        self.op["capture_failure"] = "CAPTURE_LOG_LIMIT_EXCEEDED"
        self.write()
        with self.assertRaisesRegex(CrosscheckError, "OPERATOR_FAILURE"):
            crosscheck(self.source, self.evidence)

    def test_disk_reserve_breach_rejected(self):
        self.obs[1]["free_storage_bytes"] = 3_000_000_000
        write_trace(self.trace, self.obs)
        self.op["before"] = self.obs[0]
        self.op["after"] = self.obs[-1]
        self.write()
        with self.assertRaisesRegex(CrosscheckError, "DISK_RESERVE_BREACH"):
            crosscheck(self.source, self.evidence)

    def test_wrong_trace_sample_count_rejected(self):
        d = json.loads(self.context_path.read_text())
        d["trace_samples"] = 999
        self.context_path.write_text(json.dumps(d))
        with self.assertRaisesRegex(CrosscheckError, "TRACE_SAMPLE_COUNT"):
            crosscheck(self.source, self.evidence)

    def test_context_schema_rejected(self):
        d = json.loads(self.context_path.read_text())
        d["schema"] = "v25"
        self.context_path.write_text(json.dumps(d))
        with self.assertRaisesRegex(CrosscheckError, "V26_CONTEXT_SCHEMA"):
            crosscheck(self.source, self.evidence)

    def test_source_symlink_rejected(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(CrosscheckError, "UNSAFE_SOURCE"):
            crosscheck(link, self.evidence)
