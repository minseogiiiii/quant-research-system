from __future__ import annotations

import json
from pathlib import Path

from world_quant_system.toss_live_read.models import (
    TossLiveReadConfigurationError,
    TossLiveReadPolicy,
)


def load_policy(path: str | Path) -> TossLiveReadPolicy:
    document = _load_object(path)
    allowed = {
        "provider",
        "base_url",
        "pinned_openapi_version",
        "required_account_type",
        "timeout_seconds",
        "maximum_response_bytes",
        "maximum_read_attempts",
        "maximum_retry_delay_seconds",
        "closed_order_lookback_days",
        "closed_order_page_size",
        "maximum_closed_order_pages",
        "enable_variable",
        "enable_value",
        "redirects_enabled",
        "proxy_inheritance_enabled",
        "broker_writes_enabled",
        "token_persistence_enabled",
        "account_persistence_enabled",
    }
    unknown = set(document) - allowed
    if unknown:
        raise TossLiveReadConfigurationError(
            "Unknown live-read policy fields: " + ", ".join(sorted(unknown))
        )
    return TossLiveReadPolicy(
        provider=_text(document, "provider"),
        base_url=_text(document, "base_url"),
        pinned_openapi_version=_text(
            document,
            "pinned_openapi_version",
        ),
        required_account_type=_text(document, "required_account_type"),
        timeout_seconds=_number(document, "timeout_seconds"),
        maximum_response_bytes=_integer(document, "maximum_response_bytes"),
        maximum_read_attempts=_integer(document, "maximum_read_attempts"),
        maximum_retry_delay_seconds=_number(
            document,
            "maximum_retry_delay_seconds",
        ),
        closed_order_lookback_days=_integer(
            document,
            "closed_order_lookback_days",
        ),
        closed_order_page_size=_integer(document, "closed_order_page_size"),
        maximum_closed_order_pages=_integer(
            document,
            "maximum_closed_order_pages",
        ),
        enable_variable=_text(document, "enable_variable"),
        enable_value=_text(document, "enable_value"),
        redirects_enabled=_boolean(document, "redirects_enabled"),
        proxy_inheritance_enabled=_boolean(
            document,
            "proxy_inheritance_enabled",
        ),
        broker_writes_enabled=_boolean(document, "broker_writes_enabled"),
        token_persistence_enabled=_boolean(
            document,
            "token_persistence_enabled",
        ),
        account_persistence_enabled=_boolean(
            document,
            "account_persistence_enabled",
        ),
    )


def _load_object(path: str | Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TossLiveReadConfigurationError(
            f"Unable to load live-read policy: {path}"
        ) from error
    if not isinstance(value, dict):
        raise TossLiveReadConfigurationError(
            "Live-read policy must be an object."
        )
    return value


def _text(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TossLiveReadConfigurationError(
            f"{key} must be nonblank text."
        )
    return value


def _integer(document: dict[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TossLiveReadConfigurationError(f"{key} must be an integer.")
    return value


def _number(document: dict[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TossLiveReadConfigurationError(f"{key} must be numeric.")
    return float(value)


def _boolean(document: dict[str, object], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise TossLiveReadConfigurationError(f"{key} must be boolean.")
    return value
