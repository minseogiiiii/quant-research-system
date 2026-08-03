from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.broker_certification.ingestion import (
    load_capture_bundle,
    load_policy,
)
from world_quant_system.broker_certification.models import (
    AdapterCertificationReport,
    CertificationStatus,
)
from world_quant_system.broker_certification.service import (
    DeterministicReadOnlyAdapterCertifier,
)

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / "examples/toss_read_only_certification_policy.example.json"
CAPTURES = ROOT / "examples/toss_read_only_capture_bundle.example.json"
CERTIFIED_AT = datetime(2026, 8, 3, 21, 30, tzinfo=UTC)


def _certify() -> AdapterCertificationReport:
    return DeterministicReadOnlyAdapterCertifier().certify(
        policy=load_policy(POLICY),
        capture=load_capture_bundle(CAPTURES),
        certified_at=CERTIFIED_AT,
    )


def test_synthetic_capture_bundle_passes() -> None:
    report = _certify()
    assert report.status is CertificationStatus.PASS
    assert len(report.operations) == 4
    assert len(report.accounts) == 1
    assert len(report.holdings) == 1
    assert len(report.orders) == 2
    assert not report.network_transport_enabled
    assert not report.write_operations_enabled


def test_report_is_deterministic() -> None:
    first = _certify()
    second = _certify()
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest


def test_unknown_order_status_fails_closed(tmp_path: Path) -> None:
    document = json.loads(CAPTURES.read_text(encoding="utf-8"))
    document["operations"]["open_orders"][0]["body"]["result"]["orders"][0][
        "status"
    ] = "MYSTERY"
    path = tmp_path / "captures.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    report = DeterministicReadOnlyAdapterCertifier().certify(
        policy=load_policy(POLICY),
        capture=load_capture_bundle(path),
        certified_at=CERTIFIED_AT,
    )
    assert report.status is CertificationStatus.FAIL
    assert any(
        finding.code == "UNKNOWN_ORDER_STATUS" for finding in report.findings
    )


def test_account_fingerprint_mismatch_fails(tmp_path: Path) -> None:
    document = json.loads(POLICY.read_text(encoding="utf-8"))
    document["expected_account_fingerprint"] = "f" * 64
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    report = DeterministicReadOnlyAdapterCertifier().certify(
        policy=load_policy(path),
        capture=load_capture_bundle(CAPTURES),
        certified_at=CERTIFIED_AT,
    )
    assert report.status is CertificationStatus.FAIL
    assert any(
        finding.code == "ACCOUNT_FINGERPRINT_MISMATCH"
        for finding in report.findings
    )


def test_missing_request_id_fails_when_required(tmp_path: Path) -> None:
    document = json.loads(CAPTURES.read_text(encoding="utf-8"))
    headers = document["operations"]["accounts"][0]["headers"]
    del headers["X-Request-Id"]
    path = tmp_path / "captures.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    report = DeterministicReadOnlyAdapterCertifier().certify(
        policy=load_policy(POLICY),
        capture=load_capture_bundle(path),
        certified_at=CERTIFIED_AT,
    )
    assert report.status is CertificationStatus.FAIL
    assert any(finding.code == "REQUEST_ID_MISSING" for finding in report.findings)


def test_duplicate_capture_documents_are_not_mutated() -> None:
    original = json.loads(CAPTURES.read_text(encoding="utf-8"))
    copied = copy.deepcopy(original)
    _certify()
    assert original == copied
