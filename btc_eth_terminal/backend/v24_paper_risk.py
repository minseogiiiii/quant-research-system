"""Deterministic, synthetic-fixture-only BTC/ETH paper risk controls.

NO broker, internet, order adapter, live trade, or empirical exchange claims.
The risk parameters are illustrative policy inputs, NOT measured venue rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ASSETS = ("BTC-USD", "ETH-USD")
FIXTURE = "SYNTHETIC_FIXTURE_ONLY"
ZERO = Decimal("0")
BP = Decimal("10000")


class PaperReject(ValueError):
    """An explicit, fail-closed paper safety or state rejection."""


def dec(
    value, label: str, *, positive: bool = False, allow_zero: bool = False
) -> Decimal:
    """Reject floats, bools, nonfinite decimals and precision mistakes."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PaperReject(f"{label}: use an exact decimal string, integer or Decimal")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaperReject(f"{label}: malformed decimal") from exc
    if not d.is_finite() or len(d.as_tuple().digits) > 28:
        raise PaperReject(f"{label}: nonfinite or excessive precision")
    if positive and d <= ZERO:
        raise PaperReject(f"{label}: must be positive")
    if not positive and not allow_zero and d < ZERO:
        raise PaperReject(f"{label}: cannot be negative")
    return d


def integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value >= 2**63:
        raise PaperReject(f"{label}: expected integer >= {minimum}")
    return value


@dataclass(frozen=True)
class PaperPolicy:
    starting_cash_usd: str = "10000"
    max_order_usd: str = "1000"
    max_gross_usd: str = "5000"
    max_symbol_usd: str = "3000"
    max_drawdown_usd: str = "250"
    fee_bps: str = "10"
    max_staleness_ms: int = 2000
    max_open_orders: int = 8

    def __post_init__(self) -> None:
        for name in (
            "starting_cash_usd",
            "max_order_usd",
            "max_gross_usd",
            "max_symbol_usd",
            "max_drawdown_usd",
        ):
            dec(getattr(self, name), name, positive=True)
        fees = dec(self.fee_bps, "fee_bps")
        if fees > 1000:
            raise PaperReject("fee_bps: implausible synthetic policy")
        integer(self.max_staleness_ms, "max_staleness_ms", minimum=1)
        integer(self.max_open_orders, "max_open_orders", minimum=1)
        if self.max_open_orders > 10000:
            raise PaperReject("max_open_orders: resource limit")

    def mapping(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @property
    def fee_factor(self) -> Decimal:
        return dec(self.fee_bps, "fee_bps") / BP


@dataclass(frozen=True)
class FixtureQuote:
    instrument: str
    bid: str
    ask: str
    received_ms: int
    decision_ms: int
    integrity: str = "HEALTHY"
    origin: str = FIXTURE

    def validate(self, policy: PaperPolicy) -> tuple[Decimal, Decimal]:
        if self.origin != FIXTURE:
            raise PaperReject(
                "UNVERIFIED_FEED: paper fixture engine refuses external source"
            )
        if self.integrity != "HEALTHY":
            raise PaperReject("INTEGRITY_FAILED: quarantine or stale source")
        if self.instrument not in ASSETS:
            raise PaperReject("INSTRUMENT_UNSUPPORTED")
        integer(self.received_ms, "received_ms")
        integer(self.decision_ms, "decision_ms")
        if not 0 <= self.decision_ms - self.received_ms <= policy.max_staleness_ms:
            raise PaperReject("STALE_OR_FUTURE_QUOTE")
        bid = dec(self.bid, "bid", positive=True)
        ask = dec(self.ask, "ask", positive=True)
        if bid >= ask:
            raise PaperReject("CROSSED_OR_LOCKED_BOOK")
        return bid, ask


def validate_intent(
    *,
    instrument: str,
    side: str,
    quantity: str,
    limit_price: str,
    quote: FixtureQuote,
    account: dict,
    policy: PaperPolicy,
) -> tuple[Decimal, Decimal]:
    """Conservative reservation checks; NO execution probability implied.

    ``account`` is solely reconstructed from the local immutable-event ledger.
    Buy reservations include hypothetical max fees; no margin/short selling.
    """
    if side not in ("BUY", "SELL") or instrument not in ASSETS:
        raise PaperReject("SIDE_OR_INSTRUMENT_INVALID")
    bid, ask = quote.validate(policy)
    if quote.instrument != instrument:
        raise PaperReject("CROSS_INSTRUMENT_QUOTE")
    qty = dec(quantity, "quantity", positive=True)
    px = dec(limit_price, "limit_price", positive=True)
    if qty.as_tuple().exponent < -8 or px.as_tuple().exponent < -8:
        raise PaperReject("SYNTHETIC_PRECISION_LIMIT")
    notional = qty * px
    if notional > dec(policy.max_order_usd, "max_order_usd"):
        raise PaperReject("MAX_ORDER_NOTIONAL")
    if account["mode"] != "NORMAL":
        raise PaperReject("RISK_NOT_NORMAL")
    if account["open_orders"] >= policy.max_open_orders:
        raise PaperReject("MAX_OPEN_ORDERS")
    if account["drawdown_usd"] > dec(policy.max_drawdown_usd, "max_drawdown_usd"):
        raise PaperReject("DRAWDOWN_LIMIT")
    positions = account["positions"]
    reservations = account["reservations"]
    if side == "BUY":
        needed = notional * (Decimal("1") + policy.fee_factor)
        if needed > account["cash_usd"] - reservations["cash_usd"]:
            raise PaperReject("INSUFFICIENT_UNRESERVED_CASH")
        # Include pending buy orders and the proposed order at synthetic bid value.
        # Value the new liability at the worse of its limit and the quote.
        # A high buy limit MUST NOT bypass exposure via a low current ask.
        projected_value = (
            positions[instrument] * ask
            + reservations["buy_notional_usd"][instrument]
            + qty * max(px, ask)
        )
        if projected_value > dec(policy.max_symbol_usd, "max_symbol_usd"):
            raise PaperReject("MAX_SYMBOL_EXPOSURE")
        other = next(x for x in ASSETS if x != instrument)
        total = (
            projected_value
            + positions[other] * account["marks"][other]
            + reservations["buy_notional_usd"][other]
        )
        if total > dec(policy.max_gross_usd, "max_gross_usd"):
            raise PaperReject("MAX_GROSS_EXPOSURE")
    else:
        if qty > positions[instrument] - reservations["sell_qty"][instrument]:
            raise PaperReject("NO_SHORT_OR_DOUBLE_RESERVED_SELL")
    return qty, px
