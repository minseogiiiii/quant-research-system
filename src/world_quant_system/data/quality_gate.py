from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from world_quant_system.data.quality_models import (
    DataQualityConfigurationError,
    DataQualityReport,
    DataQualityReportWriter,
    QualityAssessment,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    build_assessment_key,
    status_from_issues,
)
from world_quant_system.data.quality_validator import DataQualityValidator
from world_quant_system.data.raw_market_data import (
    RawMarketDataError,
    RawMarketDataMetadata,
)
from world_quant_system.domain.models import CandlePage, Quote


class QualityClock(Protocol):
    def now_utc(self) -> datetime:
        """Return a timezone-aware current time."""
        ...


class SystemQualityClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class RawMarketDataVerifier(Protocol):
    async def verify(self, record_id: str) -> RawMarketDataMetadata:
        """Verify one raw record and return trusted catalog metadata."""
        ...


class MarketDataQualityGate:
    """Verify raw bytes, classify semantic risk, and persist every decision."""

    def __init__(
        self,
        raw_store: RawMarketDataVerifier,
        report_store: DataQualityReportWriter,
        *,
        validator: DataQualityValidator | None = None,
        clock: QualityClock | None = None,
    ) -> None:
        self._raw_store = raw_store
        self._report_store = report_store
        self._validator = validator or DataQualityValidator()
        self._clock = clock or SystemQualityClock()

    async def assess_quotes(
        self,
        metadata: RawMarketDataMetadata,
        quotes: Sequence[Quote],
    ) -> DataQualityReport:
        assessment = self._validator.assess_quotes(
            quotes,
            now_utc=metadata.captured_at.astimezone(UTC),
            expected_source=metadata.provider,
        )
        return await self._finalize(
            metadata=metadata,
            expected_endpoint="/api/v1/prices",
            assessment=assessment,
        )

    async def assess_candle_page(
        self,
        metadata: RawMarketDataMetadata,
        page: CandlePage,
    ) -> DataQualityReport:
        assessment = self._validator.assess_candle_page(
            page,
            now_utc=metadata.captured_at.astimezone(UTC),
            expected_source=metadata.provider,
        )
        return await self._finalize(
            metadata=metadata,
            expected_endpoint="/api/v1/candles",
            assessment=assessment,
        )

    async def assess_parse_failure(
        self,
        metadata: RawMarketDataMetadata,
        *,
        expected_endpoint: str,
    ) -> DataQualityReport:
        return await self._finalize(
            metadata=metadata,
            expected_endpoint=expected_endpoint,
            assessment=self._validator.parse_failure_assessment(),
        )

    async def _finalize(
        self,
        *,
        metadata: RawMarketDataMetadata,
        expected_endpoint: str,
        assessment: QualityAssessment,
    ) -> DataQualityReport:
        issues = list(assessment.issues)
        try:
            verified = await self._raw_store.verify(metadata.record_id)
        except RawMarketDataError:
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.RAW_INTEGRITY_FAILURE,
                    severity=QualitySeverity.QUARANTINE,
                    message="Raw market-data bytes failed integrity verification.",
                )
            )
        else:
            if verified != metadata:
                issues.append(
                    QualityIssue(
                        code=QualityIssueCode.RAW_METADATA_MISMATCH,
                        severity=QualitySeverity.QUARANTINE,
                        message=(
                            "Raw market-data metadata changed between capture and "
                            "quality assessment."
                        ),
                    )
                )

        if metadata.endpoint != expected_endpoint:
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.ENDPOINT_MISMATCH,
                    severity=QualitySeverity.QUARANTINE,
                    message="Raw endpoint did not match the assessed dataset kind.",
                    details={
                        "expected_endpoint": expected_endpoint,
                        "actual_endpoint": metadata.endpoint,
                    },
                )
            )

        issue_tuple = tuple(issues)
        checked_at = self._now_utc()
        report = DataQualityReport(
            report_id=str(uuid4()),
            assessment_key=build_assessment_key(
                record_id=metadata.record_id,
                raw_content_sha256=metadata.content_sha256,
                dataset_kind=assessment.dataset_kind,
                validator_version=self._validator.version,
                policy_fingerprint=self._validator.policy_fingerprint,
            ),
            record_id=metadata.record_id,
            raw_content_sha256=metadata.content_sha256,
            dataset_kind=assessment.dataset_kind,
            status=status_from_issues(issue_tuple),
            checked_at=checked_at,
            validator_version=self._validator.version,
            policy_fingerprint=self._validator.policy_fingerprint,
            item_count=assessment.item_count,
            issues=issue_tuple,
        )
        return await self._report_store.save(report)

    def _now_utc(self) -> datetime:
        now = self._clock.now_utc()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise DataQualityConfigurationError(
                "Quality clock must return a timezone-aware datetime."
            )
        return now.astimezone(UTC)
