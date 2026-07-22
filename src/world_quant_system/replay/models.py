from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from world_quant_system.data.normalized_models import (
    NormalizedCandleRecord,
    canonical_json_bytes,
    format_utc,
)
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.domain.models import CandleInterval


class ReplayError(Exception):
    """Base exception for deterministic replay failures."""


class ReplayConfigurationError(ReplayError):
    """Raised when replay configuration is invalid."""


class ReplayInvariantError(ReplayError):
    """Raised when replay data violates chronological invariants."""


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    symbols: tuple[str, ...]
    interval: CandleInterval
    start: datetime | None = None
    end: datetime | None = None
    include_warnings: bool = True
    page_size: int = 1_000

    def __post_init__(self) -> None:
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise ReplayConfigurationError(
                "Replay symbols must be a nonempty tuple."
            )
        normalized_symbols: list[str] = []
        seen: set[str] = set()
        for symbol in self.symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ReplayConfigurationError(
                    "Replay symbols must be nonblank strings."
                )
            normalized = symbol.strip().upper()
            if normalized in seen:
                raise ReplayConfigurationError(
                    "Replay symbols cannot contain duplicates."
                )
            seen.add(normalized)
            normalized_symbols.append(normalized)
        object.__setattr__(self, "symbols", tuple(sorted(normalized_symbols)))

        if not isinstance(self.interval, CandleInterval):
            raise ReplayConfigurationError(
                "Replay interval must be a CandleInterval value."
            )
        _require_optional_aware(self.start, "Replay start")
        _require_optional_aware(self.end, "Replay end")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ReplayConfigurationError(
                "Replay start cannot be later than replay end."
            )
        if not isinstance(self.include_warnings, bool):
            raise ReplayConfigurationError(
                "Replay include-warnings flag must be a boolean."
            )
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= 10_000
        ):
            raise ReplayConfigurationError(
                "Replay page size must be between 1 and 10000."
            )

    @property
    def statuses(self) -> tuple[QualityStatus, ...]:
        if self.include_warnings:
            return (QualityStatus.PASS, QualityStatus.WARNING)
        return (QualityStatus.PASS,)

    @property
    def fingerprint(self) -> str:
        document = {
            "symbols": list(self.symbols),
            "interval": self.interval.value,
            "start": format_utc(self.start) if self.start is not None else None,
            "end": format_utc(self.end) if self.end is not None else None,
            "include_warnings": self.include_warnings,
            "page_size": self.page_size,
        }
        return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    sequence: int
    event_time: datetime
    record: NormalizedCandleRecord

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ReplayConfigurationError(
                "Replay event sequence must be a nonnegative integer."
            )
        _require_aware(self.event_time, "Replay event time")
        if not isinstance(self.record, NormalizedCandleRecord):
            raise ReplayConfigurationError(
                "Replay event must contain a normalized candle record."
            )
        if self.event_time.astimezone(UTC) != self.record.candle.timestamp.astimezone(
            UTC
        ):
            raise ReplayConfigurationError(
                "Replay event time must match the candle timestamp."
            )


@dataclass(frozen=True, slots=True)
class ReplayRunResult:
    config_fingerprint: str
    event_digest: str
    event_count: int
    pass_count: int
    warning_count: int
    first_event_at: datetime | None
    last_event_at: datetime | None

    def __post_init__(self) -> None:
        for digest_name, digest_value in (
            ("Config fingerprint", self.config_fingerprint),
            ("Event digest", self.event_digest),
        ):
            if (
                not isinstance(digest_value, str)
                or len(digest_value) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest_value
                )
            ):
                raise ReplayConfigurationError(
                    f"{digest_name} must be a lowercase SHA-256 digest."
                )
        for count_name, count_value in (
            ("Event count", self.event_count),
            ("PASS count", self.pass_count),
            ("WARNING count", self.warning_count),
        ):
            if (
                isinstance(count_value, bool)
                or not isinstance(count_value, int)
                or count_value < 0
            ):
                raise ReplayConfigurationError(
                    f"{count_name} must be a nonnegative integer."
                )
        if self.pass_count + self.warning_count != self.event_count:
            raise ReplayConfigurationError(
                "Replay quality counts must equal the event count."
            )
        _require_optional_aware(self.first_event_at, "First event time")
        _require_optional_aware(self.last_event_at, "Last event time")
        if self.event_count == 0:
            if self.first_event_at is not None or self.last_event_at is not None:
                raise ReplayConfigurationError(
                    "Empty replay cannot contain event timestamps."
                )
        elif self.first_event_at is None or self.last_event_at is None:
            raise ReplayConfigurationError(
                "Nonempty replay must contain first and last timestamps."
            )
        elif self.first_event_at > self.last_event_at:
            raise ReplayConfigurationError(
                "Replay first timestamp cannot follow the last timestamp."
            )


def replay_event_document(event: ReplayEvent) -> dict[str, object]:
    candle = event.record.candle
    return {
        "sequence": event.sequence,
        "event_time": format_utc(event.event_time),
        "item_id": event.record.item_id,
        "content_sha256": event.record.content_sha256,
        "quality_status": event.record.quality_status.value,
        "symbol": candle.symbol,
        "interval": candle.interval.value,
        "source": candle.source,
    }


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ReplayConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)
