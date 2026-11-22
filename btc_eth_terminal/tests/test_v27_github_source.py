"""Real GitHub artifact contract and adversarial immutability tests.

The existing real 60s source is frozen. No synthetic packets are used to claim
exchange observations or execution alpha.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from v27_github_public_smoke_audit import (  # noqa: E402
    ARTIFACT_SHA,
    MANIFEST_SHA,
    SOURCE_SUBDIR,
    V27AuditError,
    digest,
    verify,
    verify_artifact_bundle,
    verify_manifest_contract,
)

EVIDENCE = ROOT / "evidence/v27_github_real_60s"


def original_contract():
    path = EVIDENCE / "source" / SOURCE_SUBDIR / "manifest.json"
    return json.loads(path.read_text()), json.loads(
        (EVIDENCE / "v27_workflow_byte_check.json").read_text()
    )


def test_real_frozen_artifact_sha_and_manifest_sha():
    assert digest(EVIDENCE / "github_workflow_artifact.zip") == ARTIFACT_SHA
    assert digest(EVIDENCE / "source" / SOURCE_SUBDIR / "manifest.json") == MANIFEST_SHA


def test_extracted_bytes_match_exact_original_github_artifact():
    assert verify_artifact_bundle(EVIDENCE)[
        "extracted_bytes_identical_to_github_artifact"
    ]


def test_full_actual_source_two_independent_replays_match():
    report = verify(EVIDENCE)
    assert (
        report["status"] == "REAL_PUBLIC_60S_SOURCE_ARCHIVED_INTERNALLY_DOUBLE_REPLAYED"
    )
    assert (report["messages"], report["chunks"], report["connections"]) == (2706, 2, 1)
    assert report["verified_independent_software_replay_of_same_source"]
    assert not report["v26_host_monitored_clock_evidence_available"]
    assert not report["research_blind_oos_eligible"]
    assert not report["confirmed_executable_alpha"]
    assert report["real_orders_sent"] == 0


def test_detect_manifest_incorrect_status_or_injected_fault():
    manifest, check = original_contract()
    for field, new_value in [
        ("status", "CAPTURING_UNVERIFIED"),
        ("faults", [{"reason": "DISCONNECTED"}]),
        ("total_reconnects", 1),
        ("producer_release", "v20_multiday_bounded"),
        ("orders_enabled", True),
        ("ready_for_research", False),
    ]:
        altered = deepcopy(manifest)
        altered[field] = new_value
        with pytest.raises(V27AuditError):
            verify_manifest_contract(altered, check)


def test_detect_false_workflow_report():
    manifest, check = original_contract()
    for field, new_value in [
        ("message_count", 2705),
        ("order_count", 1),
        ("independent_exchange_truth", True),
        ("manifest_sha256", "0" * 64),
    ]:
        altered = deepcopy(check)
        altered[field] = new_value
        with pytest.raises(V27AuditError):
            verify_manifest_contract(manifest, altered)


def test_wrong_artifact_location_fails_closed(tmp_path):
    with pytest.raises(V27AuditError):
        verify_artifact_bundle(tmp_path)


def test_wrong_full_artifact_hash_fails_closed(tmp_path):
    (tmp_path / "github_workflow_artifact.zip").write_bytes(b"fake zip")
    with pytest.raises(V27AuditError, match="GITHUB_ARTIFACT_SHA_MISMATCH"):
        verify_artifact_bundle(tmp_path)


def test_old_v26_host_gate_not_promoted_as_actual_24h():
    from v25_capture_admission import assess_archive

    source = EVIDENCE / "source" / SOURCE_SUBDIR
    result = assess_archive(source)
    assert result["status"] == "INTERNAL_REPLAY_ONLY_HOST_EVIDENCE_INCOMPLETE"
    assert not result["candidate_24h_internal_source"]
    assert not result["candidate_real_60s_smoke"]
    assert (
        "LEGACY_PRODUCER_NOT_V25_RUN_ELIGIBLE" in result["operator_evidence_failures"]
    )
