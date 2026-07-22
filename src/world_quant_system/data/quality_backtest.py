from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.data.quality_validator import DataQualityValidator
from world_quant_system.domain.models import Candle, CandleInterval, CandlePage, Quote


@dataclass(frozen=True, slots=True)
class QualityBacktestResult:
    total_cases: int
    exact_matches: int
    unsafe_cases: int
    unsafe_blocked: int
    false_quarantines: int
    alpha_preservation_cases: int
    alpha_preserved: int

    @property
    def accuracy(self) -> float:
        return self.exact_matches / self.total_cases

    @property
    def unsafe_recall(self) -> float:
        return self.unsafe_blocked / self.unsafe_cases


@dataclass(slots=True)
class _Counters:
    total_cases: int = 0
    exact_matches: int = 0
    unsafe_cases: int = 0
    unsafe_blocked: int = 0
    false_quarantines: int = 0
    alpha_preservation_cases: int = 0
    alpha_preserved: int = 0

    def observe(
        self,
        *,
        expected: QualityStatus,
        actual: QualityStatus,
        alpha_preservation: bool = False,
    ) -> None:
        self.total_cases += 1
        if actual is expected:
            self.exact_matches += 1
        if expected is QualityStatus.QUARANTINE:
            self.unsafe_cases += 1
            if actual is QualityStatus.QUARANTINE:
                self.unsafe_blocked += 1
        elif actual is QualityStatus.QUARANTINE:
            self.false_quarantines += 1
        if alpha_preservation:
            self.alpha_preservation_cases += 1
            if actual is not QualityStatus.QUARANTINE:
                self.alpha_preserved += 1


def run_quality_gate_backtest(
    *,
    seed: int = 20260721,
    cases_per_bucket: int = 2_000,
) -> QualityBacktestResult:
    """Run deterministic synthetic data-quality regression scenarios.

    This validates quality classifications and alpha-preserving warning policy.
    It is not an investment-strategy return backtest.
    """
    if isinstance(cases_per_bucket, bool) or not isinstance(cases_per_bucket, int):
        raise ValueError("Backtest bucket size must be an integer.")
    if cases_per_bucket < 1:
        raise ValueError("Backtest bucket size must be positive.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Backtest seed must be an integer.")

    randomizer = random.Random(seed)
    validator = DataQualityValidator()
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    counters = _Counters()

    for index in range(cases_per_bucket):
        base = Decimal(randomizer.randint(1_000, 100_000))
        jitter = timedelta(seconds=randomizer.randint(0, 600))

        clean_quote = Quote(
            symbol=f"S{index}",
            price=base,
            timestamp=now - jitter,
            currency="USD",
            source="toss",
        )
        _observe_quotes(
            counters,
            validator,
            now,
            (clean_quote,),
            QualityStatus.PASS,
        )

        stale_quote = Quote(
            symbol=f"W{index}",
            price=base,
            timestamp=now - timedelta(hours=2),
            currency="USD",
            source="toss",
        )
        _observe_quotes(
            counters,
            validator,
            now,
            (stale_quote,),
            QualityStatus.WARNING,
        )

        future_quote = Quote(
            symbol=f"Q{index}",
            price=base,
            timestamp=now + timedelta(minutes=10),
            currency="USD",
            source="toss",
        )
        _observe_quotes(
            counters,
            validator,
            now,
            (future_quote,),
            QualityStatus.QUARANTINE,
        )

        wrong_source = Quote(
            symbol=f"X{index}",
            price=base,
            timestamp=now,
            currency="USD",
            source="other",
        )
        _observe_quotes(
            counters,
            validator,
            now,
            (wrong_source,),
            QualityStatus.QUARANTINE,
        )

        _observe_quotes(
            counters,
            validator,
            now,
            (),
            QualityStatus.WARNING,
        )

        clean_candle = _candle(
            timestamp=now - timedelta(minutes=1),
            price=base,
        )
        _observe_candles(
            counters,
            validator,
            now,
            (clean_candle,),
            QualityStatus.PASS,
        )

        previous = _candle(
            timestamp=now - timedelta(minutes=2),
            price=base,
        )
        jump = _candle(
            timestamp=now - timedelta(minutes=1),
            price=base * Decimal("1.75"),
        )
        _observe_candles(
            counters,
            validator,
            now,
            (previous, jump),
            QualityStatus.WARNING,
            alpha_preservation=True,
        )

        wide_range = _candle(
            timestamp=now - timedelta(minutes=1),
            price=base,
            high_price=base * Decimal("1.80"),
            low_price=base * Decimal("0.90"),
        )
        _observe_candles(
            counters,
            validator,
            now,
            (wide_range,),
            QualityStatus.WARNING,
            alpha_preservation=True,
        )

        before_gap = _candle(
            timestamp=now - timedelta(minutes=12),
            price=base,
        )
        after_gap = _candle(
            timestamp=now - timedelta(minutes=1),
            price=base,
        )
        _observe_candles(
            counters,
            validator,
            now,
            (before_gap, after_gap),
            QualityStatus.WARNING,
            alpha_preservation=True,
        )

        zero_volume = tuple(
            _candle(
                timestamp=now - timedelta(minutes=3 - offset),
                price=base + Decimal(offset),
                volume=0,
            )
            for offset in range(3)
        )
        _observe_candles(
            counters,
            validator,
            now,
            zero_volume,
            QualityStatus.WARNING,
        )

        flatline = tuple(
            _candle(
                timestamp=now - timedelta(minutes=5 - offset),
                price=base,
                volume=1_000,
            )
            for offset in range(5)
        )
        _observe_candles(
            counters,
            validator,
            now,
            flatline,
            QualityStatus.WARNING,
        )

        future_candle = _candle(
            timestamp=now + timedelta(minutes=10),
            price=base,
        )
        _observe_candles(
            counters,
            validator,
            now,
            (future_candle,),
            QualityStatus.QUARANTINE,
        )

    return QualityBacktestResult(
        total_cases=counters.total_cases,
        exact_matches=counters.exact_matches,
        unsafe_cases=counters.unsafe_cases,
        unsafe_blocked=counters.unsafe_blocked,
        false_quarantines=counters.false_quarantines,
        alpha_preservation_cases=counters.alpha_preservation_cases,
        alpha_preserved=counters.alpha_preserved,
    )


def _observe_quotes(
    counters: _Counters,
    validator: DataQualityValidator,
    now: datetime,
    quotes: tuple[Quote, ...],
    expected: QualityStatus,
) -> None:
    assessment = validator.assess_quotes(
        quotes,
        now_utc=now,
        expected_source="toss",
    )
    counters.observe(expected=expected, actual=assessment.status)


def _observe_candles(
    counters: _Counters,
    validator: DataQualityValidator,
    now: datetime,
    candles: tuple[Candle, ...],
    expected: QualityStatus,
    *,
    alpha_preservation: bool = False,
) -> None:
    assessment = validator.assess_candle_page(
        CandlePage(candles=candles, next_before=None),
        now_utc=now,
        expected_source="toss",
    )
    counters.observe(
        expected=expected,
        actual=assessment.status,
        alpha_preservation=alpha_preservation,
    )


def _candle(
    *,
    timestamp: datetime,
    price: Decimal,
    high_price: Decimal | None = None,
    low_price: Decimal | None = None,
    volume: int = 1_000,
) -> Candle:
    return Candle(
        symbol="TEST",
        interval=CandleInterval.MINUTE_1,
        timestamp=timestamp,
        open_price=price,
        high_price=high_price or price,
        low_price=low_price or price,
        close_price=price,
        volume=volume,
        currency="USD",
        source="toss",
    )
