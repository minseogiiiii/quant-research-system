from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.broker_certification.ingestion import (
    load_capture_bundle,
    load_policy,
)
from world_quant_system.broker_certification.models import CertificationStatus
from world_quant_system.broker_certification.reporting import (
    AtomicJsonAdapterCertificationReportWriter,
)
from world_quant_system.broker_certification.service import (
    DeterministicReadOnlyAdapterCertifier,
)


def _find_project_root() -> Path:
    policy_relative_path = Path(
        "examples/toss_read_only_certification_policy.example.json"
    )
    capture_relative_path = Path(
        "examples/toss_read_only_capture_bundle.example.json"
    )
    search_roots = (Path.cwd(), *Path.cwd().parents)

    for root in search_roots:
        if (
            (root / policy_relative_path).is_file()
            and (root / capture_relative_path).is_file()
        ):
            return root

    raise FileNotFoundError(
        "Unable to locate the Toss certification example fixtures "
        "from the current working directory."
    )


def main() -> None:
    root = _find_project_root()
    policy_path = (
        root
        / "examples/toss_read_only_certification_policy.example.json"
    )
    capture_path = (
        root
        / "examples/toss_read_only_capture_bundle.example.json"
    )
    policy = load_policy(policy_path)
    capture = load_capture_bundle(capture_path)
    certified_at = datetime(2026, 8, 3, 21, 30, tzinfo=UTC)
    certifier = DeterministicReadOnlyAdapterCertifier()
    first = certifier.certify(
        policy=policy,
        capture=capture,
        certified_at=certified_at,
    )
    second = certifier.certify(
        policy=policy,
        capture=capture,
        certified_at=certified_at,
    )
    if first.status is not CertificationStatus.PASS:
        raise AssertionError("Synthetic certification fixture must pass.")
    if first.report_id != second.report_id:
        raise AssertionError("Certification report ID is not deterministic.")
    if first.report_digest != second.report_digest:
        raise AssertionError("Certification report digest is not deterministic.")
    if first.network_transport_enabled or first.write_operations_enabled:
        raise AssertionError("Certification safety boundaries were not preserved.")
    with tempfile.TemporaryDirectory() as temporary_directory:
        output = Path(temporary_directory) / "certification.json"
        AtomicJsonAdapterCertificationReportWriter(output).write(first)
        stored = json.loads(output.read_text(encoding="utf-8"))
        if stored["report_digest"] != first.report_digest:
            raise AssertionError("Stored certification report digest changed.")
        file_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        if len(file_digest) != 64:
            raise AssertionError("Stored report SHA-256 digest is invalid.")
    print("Toss read-only adapter certification simulation passed.")
    print(f"Report ID: {first.report_id}")
    print(f"Report digest: {first.report_digest}")
    print("Network transport: DISABLED")
    print("Write operations: DISABLED")


if __name__ == "__main__":
    main()
