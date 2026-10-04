from __future__ import annotations

import json
import re
from pathlib import Path

from world_quant_system.disabled_write_transport.models import (
    DisabledWriteTransportConfigurationError,
    DisabledWriteTransportPolicy,
)


def load_policy(path: str | Path) -> DisabledWriteTransportPolicy:
    document = _load_object(path)
    return DisabledWriteTransportPolicy(
        maximum_envelope_ttl_seconds=_integer(
            document,
            "maximum_envelope_ttl_seconds",
        )
    )


def load_approver_fingerprint(path: str | Path) -> str:
    document = _load_object(path)
    value = document.get("approver_fingerprint")
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise DisabledWriteTransportConfigurationError(
            "approver_fingerprint must be a SHA-256 digest."
        )
    return value


def _load_object(path: str | Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DisabledWriteTransportConfigurationError(
            f"Unable to load JSON document: {path}"
        ) from error
    if not isinstance(value, dict):
        raise DisabledWriteTransportConfigurationError(
            "Disabled-transport JSON root must be an object."
        )
    return value


def _integer(document: dict[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DisabledWriteTransportConfigurationError(
            f"{key} must be an integer."
        )
    return value
