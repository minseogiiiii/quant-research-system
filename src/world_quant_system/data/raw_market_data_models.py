from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

_SCHEMA_VERSION = 1
_CATALOG_SCHEMA_VERSION = 1
_DEFAULT_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_DEFAULT_COMPRESSION_LEVEL = 6
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "account_number",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "private_key",
    "refresh_token",
    "secret",
)
_REDACTED = "[REDACTED]"


class RawMarketDataError(Exception):
    """Base exception for raw market-data persistence failures."""


class RawMarketDataConfigurationError(RawMarketDataError):
    """Raised when raw storage configuration is invalid."""


class RawMarketDataSerializationError(RawMarketDataError):
    """Raised when a capture cannot be serialized safely."""


class RawMarketDataIntegrityError(RawMarketDataError):
    """Raised when persisted bytes do not match their catalog metadata."""


class RawMarketDataConflictError(RawMarketDataError):
    """Raised when an idempotency key is reused for different content."""


class RawMarketDataNotFoundError(RawMarketDataError):
    """Raised when a requested raw record does not exist."""


@dataclass(frozen=True, slots=True, repr=False)
class RawMarketDataCapture:
    """One broker response and the non-secret request metadata that produced it."""

    provider: str
    endpoint: str
    request_params: Mapping[str, str]
    captured_at: datetime
    status_code: int
    response_headers: Mapping[str, str]
    json_body: Mapping[str, object] | None
    text: str = ""
    request_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _validate_provider(self.provider)
        _validate_endpoint(self.endpoint)
        _require_aware_datetime(self.captured_at, "Captured-at timestamp")

        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise RawMarketDataConfigurationError(
                "Status code must be an integer between 100 and 599."
            )

        _validate_string_mapping(self.request_params, "Request parameters")
        _validate_string_mapping(self.response_headers, "Response headers")

        if self.json_body is not None and not isinstance(self.json_body, Mapping):
            raise RawMarketDataConfigurationError(
                "JSON body must be a mapping or None."
            )

        normalized_json = _normalize_json_mapping(self.json_body)
        _reject_sensitive_json(normalized_json)
        object.__setattr__(
            self,
            "request_params",
            MappingProxyType(dict(self.request_params)),
        )
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType(dict(self.response_headers)),
        )
        object.__setattr__(
            self,
            "json_body",
            _freeze_json_mapping(normalized_json),
        )

        if not isinstance(self.text, str):
            raise RawMarketDataConfigurationError("Response text must be a string.")
        _reject_sensitive_text(self.text)

        _validate_optional_nonblank(self.request_id, "Request ID")
        _validate_optional_nonblank(self.idempotency_key, "Idempotency key")

    def __repr__(self) -> str:
        return (
            "RawMarketDataCapture("
            f"provider={self.provider!r}, "
            f"endpoint={self.endpoint!r}, "
            f"captured_at={self.captured_at!r}, "
            f"status_code={self.status_code!r}, "
            f"request_param_count={len(self.request_params)}, "
            f"response_header_count={len(self.response_headers)}, "
            f"has_json_body={self.json_body is not None}, "
            f"text_length={len(self.text)}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class RawMarketDataMetadata:
    record_id: str
    provider: str
    endpoint: str
    captured_at: datetime
    status_code: int
    request_id: str | None
    idempotency_key: str | None
    content_sha256: str
    relative_path: str
    uncompressed_size: int
    compressed_size: int
    schema_version: int

    def __post_init__(self) -> None:
        _validate_record_id(self.record_id)
        _validate_provider(self.provider)
        _validate_endpoint(self.endpoint)
        _require_aware_datetime(self.captured_at, "Captured-at timestamp")
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise RawMarketDataIntegrityError("Stored status code is invalid.")
        _validate_optional_nonblank(self.request_id, "Request ID")
        _validate_optional_nonblank(self.idempotency_key, "Idempotency key")

        if not _SHA256_PATTERN.fullmatch(self.content_sha256):
            raise RawMarketDataIntegrityError("Content SHA-256 is invalid.")

        if not isinstance(self.relative_path, str) or not self.relative_path:
            raise RawMarketDataIntegrityError("Relative path is invalid.")

        for name, value in (
            ("Uncompressed size", self.uncompressed_size),
            ("Compressed size", self.compressed_size),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RawMarketDataIntegrityError(
                    f"{name} must be a nonnegative integer."
                )

        if self.schema_version != _SCHEMA_VERSION:
            raise RawMarketDataIntegrityError(
                f"Unsupported raw record schema version: {self.schema_version}."
            )


@dataclass(frozen=True, slots=True, repr=False)
class RawMarketDataRecord:
    metadata: RawMarketDataMetadata
    request_params: Mapping[str, str]
    response_headers: Mapping[str, str]
    json_body: Mapping[str, object] | None
    text: str

    def __repr__(self) -> str:
        return (
            "RawMarketDataRecord("
            f"metadata={self.metadata!r}, "
            f"request_param_count={len(self.request_params)}, "
            f"response_header_count={len(self.response_headers)}, "
            f"has_json_body={self.json_body is not None}, "
            f"text_length={len(self.text)}"
            ")"
        )


class RawMarketDataRecorder(Protocol):
    async def record(
        self,
        capture: RawMarketDataCapture,
    ) -> RawMarketDataMetadata:
        """Persist one raw capture and return immutable metadata."""
        ...


def _validate_provider(provider: object) -> None:
    if not isinstance(provider, str) or not _PROVIDER_PATTERN.fullmatch(provider):
        raise RawMarketDataConfigurationError(
            "Provider must be a lowercase storage-safe identifier."
        )


def _validate_endpoint(endpoint: object) -> None:
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("/")
        or "\x00" in endpoint
        or "?" in endpoint
        or "#" in endpoint
        or "://" in endpoint
        or len(endpoint) > 512
    ):
        raise RawMarketDataConfigurationError(
            "Endpoint must be a nonempty absolute API path."
        )


def _validate_optional_nonblank(value: object, field_name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not value.strip() or len(value) > 512
    ):
        raise RawMarketDataConfigurationError(
            f"{field_name} must be a nonblank string or None."
        )


def _validate_string_mapping(value: object, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise RawMarketDataConfigurationError(f"{field_name} must be a mapping.")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RawMarketDataConfigurationError(
                f"{field_name} must contain only string keys and values."
            )


def _require_aware_datetime(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RawMarketDataConfigurationError(
            f"{field_name} must include timezone information."
        )


def _validate_record_id(record_id: object) -> None:
    if not isinstance(record_id, str):
        raise RawMarketDataConfigurationError("Record ID must be a UUID string.")
    try:
        parsed = UUID(record_id)
    except ValueError as error:
        raise RawMarketDataConfigurationError(
            "Record ID must be a UUID string."
        ) from error
    if str(parsed) != record_id:
        raise RawMarketDataConfigurationError(
            "Record ID must use canonical UUID formatting."
        )


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_string_mapping(value: Mapping[str, str]) -> dict[str, str]:
    return {
        key: _REDACTED if _is_sensitive_key(key) else item
        for key, item in sorted(value.items(), key=lambda pair: pair[0].casefold())
    }


def _normalize_json_mapping(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    normalized = _normalize_json_value(value, path="$.")
    if not isinstance(normalized, dict):
        raise RawMarketDataSerializationError("JSON body must normalize to an object.")
    return normalized


def _normalize_json_value(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise RawMarketDataSerializationError(
                f"Non-finite JSON number is not allowed at {path}."
            )
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RawMarketDataSerializationError(
                    f"JSON object key is not a string at {path}."
                )
            normalized[key] = _normalize_json_value(item, path=f"{path}{key}.")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json_value(item, path=f"{path}[{index}].")
            for index, item in enumerate(value)
        ]
    raise RawMarketDataSerializationError(
        f"Unsupported JSON value type at {path}: {type(value).__name__}."
    )


def _reject_sensitive_json(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(key):
                raise RawMarketDataSerializationError(
                    f"Sensitive field is prohibited in raw market data at {path}.{key}."
                )
            _reject_sensitive_json(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_json(item, path=f"{path}[{index}]")


def _freeze_json_mapping(
    value: dict[str, object] | None,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    frozen = {key: _freeze_json_value(item) for key, item in value.items()}
    return MappingProxyType(frozen)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _reject_sensitive_text(value: str) -> None:
    normalized = value.casefold().replace("-", "_")
    prohibited = (
        "access_token",
        "account_number",
        "api_key",
        "authorization",
        "client_secret",
        "password",
        "private_key",
        "refresh_token",
    )
    if any(part in normalized for part in prohibited):
        raise RawMarketDataSerializationError(
            "Sensitive content is prohibited in raw market-data response text."
        )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise RawMarketDataSerializationError(
            "Raw market-data document is not valid canonical JSON."
        ) from error
    return text.encode("utf-8")


def _format_datetime(value: datetime) -> str:
    _require_aware_datetime(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RawMarketDataIntegrityError(
            "Stored timestamp is not valid ISO-8601."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RawMarketDataIntegrityError("Stored timestamp lacks timezone data.")
    return parsed.astimezone(UTC)


def _parse_document_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise RawMarketDataIntegrityError("Stored timestamp is invalid.")
    return _parse_datetime(value)


def _require_string_dict(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RawMarketDataIntegrityError(f"Stored {field_name} is not an object.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RawMarketDataIntegrityError(
                f"Stored {field_name} contains non-string data."
            )
        result[key] = item
    return result
