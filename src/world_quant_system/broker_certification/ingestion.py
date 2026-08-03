from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from world_quant_system.broker_certification.models import (
    BrokerAdapterConfigurationError,
    BrokerAdapterIntegrityError,
    ReadOnlyAdapterPolicy,
    ReadOnlyOperation,
    ReadOnlyResponse,
    parse_utc_datetime,
)


@dataclass(frozen=True, slots=True)
class CapturedOperation:
    operation: ReadOnlyOperation
    responses: tuple[ReadOnlyResponse, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ReadOnlyOperation):
            raise BrokerAdapterConfigurationError("Capture operation is invalid.")
        if not self.responses:
            raise BrokerAdapterConfigurationError(
                "Each captured operation requires at least one response."
            )


@dataclass(frozen=True, slots=True)
class ReadOnlyCaptureBundle:
    captured_at: datetime
    account_seq: str
    operations: tuple[CapturedOperation, ...]

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None:
            raise BrokerAdapterConfigurationError(
                "Capture time must be timezone-aware."
            )
        if not isinstance(self.account_seq, str) or not self.account_seq.strip():
            raise BrokerAdapterConfigurationError("Account sequence cannot be blank.")
        operation_names = [operation.operation for operation in self.operations]
        if len(set(operation_names)) != len(operation_names):
            raise BrokerAdapterIntegrityError(
                "Capture bundle contains duplicate operations."
            )

    def responses_for(
        self,
        operation: ReadOnlyOperation,
    ) -> tuple[ReadOnlyResponse, ...]:
        for captured_operation in self.operations:
            if captured_operation.operation is operation:
                return captured_operation.responses
        raise BrokerAdapterIntegrityError(
            f"Capture bundle is missing {operation.value}."
        )



def load_policy(path: str | Path) -> ReadOnlyAdapterPolicy:
    document = _load_object(path)
    required_raw = document.get("required_operations")
    if not isinstance(required_raw, list):
        raise BrokerAdapterConfigurationError(
            "required_operations must be a list."
        )
    required_operations = tuple(
        _parse_operation(value) for value in required_raw
    )
    return ReadOnlyAdapterPolicy(
        provider=_text(document.get("provider"), "provider"),
        base_url=_text(document.get("base_url"), "base_url"),
        expected_account_fingerprint=_text(
            document.get("expected_account_fingerprint"),
            "expected_account_fingerprint",
        ),
        required_operations=required_operations,
        maximum_response_bytes=_integer(
            document.get("maximum_response_bytes", 2_000_000),
            "maximum_response_bytes",
        ),
        maximum_pages_per_operation=_integer(
            document.get("maximum_pages_per_operation", 100),
            "maximum_pages_per_operation",
        ),
        maximum_attempts=_integer(
            document.get("maximum_attempts", 3),
            "maximum_attempts",
        ),
        base_backoff_seconds=_number(
            document.get("base_backoff_seconds", 0.25),
            "base_backoff_seconds",
        ),
        maximum_backoff_seconds=_number(
            document.get("maximum_backoff_seconds", 2.0),
            "maximum_backoff_seconds",
        ),
        allowed_clock_skew_seconds=_integer(
            document.get("allowed_clock_skew_seconds", 300),
            "allowed_clock_skew_seconds",
        ),
        require_request_id=_boolean(
            document.get("require_request_id", True),
            "require_request_id",
        ),
    )


def load_capture_bundle(path: str | Path) -> ReadOnlyCaptureBundle:
    document = _load_object(path)
    operations_raw = document.get("operations")
    if not isinstance(operations_raw, dict):
        raise BrokerAdapterConfigurationError("operations must be an object.")
    captured_operations: list[CapturedOperation] = []
    for operation_name, responses_raw in operations_raw.items():
        operation = _parse_operation(operation_name)
        if not isinstance(responses_raw, list) or not responses_raw:
            raise BrokerAdapterConfigurationError(
                f"{operation.value} responses must be a non-empty list."
            )
        responses = tuple(
            _parse_response(response_raw, operation)
            for response_raw in responses_raw
        )
        captured_operations.append(
            CapturedOperation(operation=operation, responses=responses)
        )
    return ReadOnlyCaptureBundle(
        captured_at=parse_utc_datetime(document.get("captured_at"), "captured_at"),
        account_seq=_text(document.get("account_seq"), "account_seq"),
        operations=tuple(
            sorted(captured_operations, key=lambda item: item.operation.value)
        ),
    )


def _parse_response(
    value: object,
    operation: ReadOnlyOperation,
) -> ReadOnlyResponse:
    if not isinstance(value, dict):
        raise BrokerAdapterConfigurationError(
            f"{operation.value} response must be an object."
        )
    headers_raw = value.get("headers", {})
    if not isinstance(headers_raw, dict):
        raise BrokerAdapterConfigurationError(
            f"{operation.value} response headers must be an object."
        )
    headers: list[tuple[str, str]] = []
    for name, header_value in headers_raw.items():
        if not isinstance(name, str) or not isinstance(header_value, str):
            raise BrokerAdapterConfigurationError(
                "Response headers must contain string keys and values."
            )
        headers.append((name, header_value))
    body = value.get("body")
    if not isinstance(body, dict):
        raise BrokerAdapterConfigurationError("Response body must be an object.")
    raw_size = value.get("raw_size_bytes")
    if raw_size is None:
        raw_size = len(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    return ReadOnlyResponse(
        status_code=_integer(value.get("status_code"), "status_code"),
        headers=tuple(sorted(headers)),
        body=body,
        received_at=parse_utc_datetime(
            value.get("received_at"),
            "received_at",
        ),
        raw_size_bytes=_integer(raw_size, "raw_size_bytes"),
    )


def _load_object(path: str | Path) -> dict[str, object]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrokerAdapterConfigurationError(
            f"Unable to load JSON document: {path}"
        ) from error
    if not isinstance(document, dict):
        raise BrokerAdapterConfigurationError("JSON document must be an object.")
    return document


def _parse_operation(value: object) -> ReadOnlyOperation:
    if not isinstance(value, str):
        raise BrokerAdapterConfigurationError("Operation name must be text.")
    try:
        return ReadOnlyOperation(value)
    except ValueError as error:
        raise BrokerAdapterConfigurationError(
            f"Unknown read-only operation: {value}"
        ) from error


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterConfigurationError(f"{field_name} must be text.")
    return value.strip()


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerAdapterConfigurationError(f"{field_name} must be an integer.")
    return value


def _number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrokerAdapterConfigurationError(f"{field_name} must be numeric.")
    return float(value)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BrokerAdapterConfigurationError(f"{field_name} must be boolean.")
    return value
