from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

from world_quant_system.data.quality_models import (
    DataQualityConfigurationError,
    DataQualityPolicy,
    QualityAssessment,
    QualityDatasetKind,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    status_from_issues,
)
from world_quant_system.domain.models import Candle, CandleInterval, CandlePage, Quote

_VALIDATOR_VERSION = "1.0.0"


class DataQualityValidator:
    """Deterministic semantic checks that preserve plausible market alpha."""

    version = _VALIDATOR_VERSION

    def __init__(self, policy: DataQualityPolicy | None = None) -> None:
        self._policy = policy or DataQualityPolicy()

    @property
    def policy(self) -> DataQualityPolicy:
        return self._policy

    @property
    def policy_fingerprint(self) -> str:
        return self._policy.fingerprint

    def assess_quotes(
        self,
        quotes: Sequence[Quote],
        *,
        now_utc: datetime,
        expected_source: str,
    ) -> QualityAssessment:
        now = self._validated_now(now_utc)
        self._validate_expected_source(expected_source)
        issues: list[QualityIssue] = []

        if not quotes:
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.EMPTY_DATASET,
                    severity=QualitySeverity.WARNING,
                    message="Quote response contained no market-data items.",
                )
            )

        for index, quote in enumerate(quotes):
            if not isinstance(quote, Quote):
                raise DataQualityConfigurationError(
                    "Quotes must contain only Quote values."
                )
            self._check_source(
                actual_source=quote.source,
                expected_source=expected_source,
                index=index,
                timestamp=quote.timestamp,
                issues=issues,
            )
            self._check_future_timestamp(
                timestamp=quote.timestamp,
                now_utc=now,
                index=index,
                issues=issues,
            )
            self._check_quote_age(
                quote=quote,
                now_utc=now,
                index=index,
                issues=issues,
            )
            self._limit_issues(issues)

        result = tuple(issues)
        return QualityAssessment(
            dataset_kind=QualityDatasetKind.QUOTES,
            item_count=len(quotes),
            issues=result,
            status=status_from_issues(result),
        )

    def assess_candle_page(
        self,
        page: CandlePage,
        *,
        now_utc: datetime,
        expected_source: str,
    ) -> QualityAssessment:
        now = self._validated_now(now_utc)
        self._validate_expected_source(expected_source)
        if not isinstance(page, CandlePage):
            raise DataQualityConfigurationError(
                "Candle assessment requires a CandlePage value."
            )

        issues: list[QualityIssue] = []
        candles = page.candles
        if not candles:
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.EMPTY_DATASET,
                    severity=QualitySeverity.WARNING,
                    message="Candle response contained no market-data items.",
                )
            )

        for index, candle in enumerate(candles):
            self._check_source(
                actual_source=candle.source,
                expected_source=expected_source,
                index=index,
                timestamp=candle.timestamp,
                issues=issues,
            )
            self._check_future_timestamp(
                timestamp=candle.timestamp,
                now_utc=now,
                index=index,
                issues=issues,
            )
            self._check_wide_range(candle, index=index, issues=issues)
            self._limit_issues(issues)

        for index, (previous, current) in enumerate(pairwise(candles), start=1):
            self._check_gap(
                previous=previous,
                current=current,
                index=index,
                issues=issues,
            )
            self._check_price_move(
                previous=previous,
                current=current,
                index=index,
                issues=issues,
            )
            self._limit_issues(issues)

        self._check_zero_volume_runs(candles, issues)
        self._check_flatline_runs(candles, issues)
        self._limit_issues(issues)

        result = tuple(issues)
        return QualityAssessment(
            dataset_kind=QualityDatasetKind.CANDLES,
            item_count=len(candles),
            issues=result,
            status=status_from_issues(result),
        )

    def parse_failure_assessment(self) -> QualityAssessment:
        issue = QualityIssue(
            code=QualityIssueCode.PARSE_FAILURE,
            severity=QualitySeverity.QUARANTINE,
            message="Raw market-data response failed strict schema parsing.",
        )
        return QualityAssessment(
            dataset_kind=QualityDatasetKind.PARSE_FAILURE,
            item_count=0,
            issues=(issue,),
            status=status_from_issues((issue,)),
        )

    def _check_source(
        self,
        *,
        actual_source: str,
        expected_source: str,
        index: int,
        timestamp: datetime,
        issues: list[QualityIssue],
    ) -> None:
        if actual_source == expected_source:
            return
        issues.append(
            QualityIssue(
                code=QualityIssueCode.SOURCE_MISMATCH,
                severity=QualitySeverity.QUARANTINE,
                message="Market-data source did not match the raw provider.",
                item_index=index,
                timestamp=timestamp,
                details={
                    "expected_source": expected_source,
                    "actual_source": actual_source,
                },
            )
        )

    def _check_future_timestamp(
        self,
        *,
        timestamp: datetime,
        now_utc: datetime,
        index: int,
        issues: list[QualityIssue],
    ) -> None:
        if timestamp.astimezone(UTC) <= now_utc + self._policy.future_tolerance:
            return
        issues.append(
            QualityIssue(
                code=QualityIssueCode.FUTURE_TIMESTAMP,
                severity=QualitySeverity.QUARANTINE,
                message="Market-data timestamp was too far in the future.",
                item_index=index,
                timestamp=timestamp,
            )
        )

    def _check_quote_age(
        self,
        *,
        quote: Quote,
        now_utc: datetime,
        index: int,
        issues: list[QualityIssue],
    ) -> None:
        age = now_utc - quote.timestamp.astimezone(UTC)
        if age <= self._policy.stale_quote_warning_after:
            return

        quarantine_after = self._policy.stale_quote_quarantine_after
        severity = QualitySeverity.WARNING
        if quarantine_after is not None and age > quarantine_after:
            severity = QualitySeverity.QUARANTINE

        issues.append(
            QualityIssue(
                code=QualityIssueCode.STALE_QUOTE,
                severity=severity,
                message="Quote timestamp exceeded the configured freshness threshold.",
                item_index=index,
                timestamp=quote.timestamp,
                details={"age_seconds": str(int(age.total_seconds()))},
            )
        )

    def _check_gap(
        self,
        *,
        previous: Candle,
        current: Candle,
        index: int,
        issues: list[QualityIssue],
    ) -> None:
        gap = current.timestamp.astimezone(UTC) - previous.timestamp.astimezone(UTC)
        should_warn = False
        if current.interval is CandleInterval.MINUTE_1:
            should_warn = (
                gap > self._policy.intraday_gap_warning_after
                and gap < self._policy.intraday_session_break_after
            )
        elif current.interval is CandleInterval.DAY_1:
            should_warn = gap > self._policy.daily_gap_warning_after

        if should_warn:
            issues.append(
                QualityIssue(
                    code=QualityIssueCode.CANDLE_GAP,
                    severity=QualitySeverity.WARNING,
                    message="Candle sequence contained an unusual time gap.",
                    item_index=index,
                    timestamp=current.timestamp,
                    details={"gap_seconds": str(int(gap.total_seconds()))},
                )
            )

    def _check_price_move(
        self,
        *,
        previous: Candle,
        current: Candle,
        index: int,
        issues: list[QualityIssue],
    ) -> None:
        move = abs((current.close_price / previous.close_price) - Decimal("1"))
        if move <= self._policy.extreme_return_warning_ratio:
            return
        issues.append(
            QualityIssue(
                code=QualityIssueCode.EXTREME_PRICE_MOVE,
                severity=QualitySeverity.WARNING,
                message=(
                    "Close-to-close move exceeded the warning threshold; "
                    "the observation remains usable for research."
                ),
                item_index=index,
                timestamp=current.timestamp,
                details={"absolute_return": format(move, "f")},
            )
        )

    def _check_wide_range(
        self,
        candle: Candle,
        *,
        index: int,
        issues: list[QualityIssue],
    ) -> None:
        width = (candle.high_price - candle.low_price) / candle.close_price
        if width <= self._policy.wide_range_warning_ratio:
            return
        issues.append(
            QualityIssue(
                code=QualityIssueCode.WIDE_CANDLE_RANGE,
                severity=QualitySeverity.WARNING,
                message=(
                    "Candle range exceeded the warning threshold; the observation "
                    "was not quarantined because legitimate volatility is possible."
                ),
                item_index=index,
                timestamp=candle.timestamp,
                details={"range_ratio": format(width, "f")},
            )
        )

    def _check_zero_volume_runs(
        self,
        candles: Sequence[Candle],
        issues: list[QualityIssue],
    ) -> None:
        threshold = self._policy.zero_volume_run_warning
        start: int | None = None
        for index, candle in enumerate((*candles, None)):
            is_zero = candle is not None and candle.volume == 0
            if is_zero and start is None:
                start = index
            if is_zero or start is None:
                continue
            run_length = index - start
            if run_length >= threshold:
                last = candles[index - 1]
                issues.append(
                    QualityIssue(
                        code=QualityIssueCode.ZERO_VOLUME_RUN,
                        severity=QualitySeverity.WARNING,
                        message="Consecutive zero-volume candles were detected.",
                        item_index=start,
                        timestamp=last.timestamp,
                        details={"run_length": str(run_length)},
                    )
                )
            start = None

    def _check_flatline_runs(
        self,
        candles: Sequence[Candle],
        issues: list[QualityIssue],
    ) -> None:
        if not candles:
            return
        threshold = self._policy.flatline_run_warning
        start = 0
        previous_key = self._candle_market_key(candles[0])
        for index in range(1, len(candles) + 1):
            current_key = (
                self._candle_market_key(candles[index])
                if index < len(candles)
                else None
            )
            if current_key == previous_key:
                continue
            run_length = index - start
            if run_length >= threshold:
                last = candles[index - 1]
                issues.append(
                    QualityIssue(
                        code=QualityIssueCode.FLATLINE_RUN,
                        severity=QualitySeverity.WARNING,
                        message=(
                            "Repeated identical OHLCV candles may indicate a stale "
                            "or frozen feed."
                        ),
                        item_index=start,
                        timestamp=last.timestamp,
                        details={"run_length": str(run_length)},
                    )
                )
            start = index
            if current_key is not None:
                previous_key = current_key

    @staticmethod
    def _candle_market_key(
        candle: Candle,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
        return (
            candle.open_price,
            candle.high_price,
            candle.low_price,
            candle.close_price,
            candle.volume,
        )

    def _limit_issues(self, issues: list[QualityIssue]) -> None:
        if len(issues) > self._policy.max_issues:
            raise DataQualityConfigurationError(
                "Quality assessment exceeded the configured issue limit."
            )

    @staticmethod
    def _validated_now(now_utc: datetime) -> datetime:
        if (
            not isinstance(now_utc, datetime)
            or now_utc.tzinfo is None
            or now_utc.utcoffset() is None
        ):
            raise DataQualityConfigurationError(
                "Quality clock must provide a timezone-aware datetime."
            )
        return now_utc.astimezone(UTC)

    @staticmethod
    def _validate_expected_source(expected_source: str) -> None:
        if not isinstance(expected_source, str) or not expected_source.strip():
            raise DataQualityConfigurationError(
                "Expected market-data source cannot be empty."
            )
