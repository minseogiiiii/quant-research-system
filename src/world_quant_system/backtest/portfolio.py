from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.interfaces import DividendTaxModel
from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    BacktestInvariantError,
    ClosedTrade,
    Fill,
    OrderSide,
    PortfolioSnapshot,
)
from world_quant_system.research.corporate_action_models import (
    CorporateActionApplication,
    CorporateActionEligibilityError,
    CorporateActionEvent,
    CorporateActionEventPhase,
    CorporateActionInvariantError,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    FractionalSharePolicy,
)

_ZERO = Decimal("0")
_TRADE_NAMESPACE = UUID("c32401d1-8264-50fe-b3f9-a1ff664025d7")
_APPLICATION_NAMESPACE = UUID("28f34537-6321-51e1-91b6-7728e8570e1f")


@dataclass(frozen=True, slots=True)
class _DividendEntitlement:
    quantity: int
    entry_at: datetime | None


class LongOnlyPortfolioLedger:
    """Decimal-based long-only ledger with replay and duplicate defenses."""

    def __init__(self, symbol: str, initial_cash: Decimal) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise BacktestConfigurationError("Portfolio symbol cannot be blank.")
        if (
            not isinstance(initial_cash, Decimal)
            or not initial_cash.is_finite()
            or initial_cash <= _ZERO
        ):
            raise BacktestConfigurationError(
                "Initial cash must be a finite positive Decimal."
            )
        self._symbol = symbol.strip().upper()
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._quantity = 0
        self._position_cost_basis = _ZERO
        self._realized_pnl = _ZERO
        self._total_commission = _ZERO
        self._total_slippage = _ZERO
        self._turnover_notional = _ZERO
        self._total_dividend_gross = _ZERO
        self._total_dividend_tax = _ZERO
        self._total_dividend_net = _ZERO
        self._total_cash_in_lieu = _ZERO
        self._corporate_action_count = 0
        self._seen_fill_ids: set[str] = set()
        self._seen_order_ids: set[str] = set()
        self._seen_corporate_event_ids: set[str] = set()
        self._dividend_entitlements: dict[str, _DividendEntitlement] = {}
        self._closed_trades: list[ClosedTrade] = []
        self._open_entry_at: datetime | None = None
        self._open_entry_cost = _ZERO
        self._open_exit_proceeds = _ZERO
        self._open_bought_quantity = 0
        self._open_sold_quantity = 0

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def initial_cash(self) -> Decimal:
        return self._initial_cash

    @property
    def closed_trades(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed_trades)

    @property
    def turnover_notional(self) -> Decimal:
        return self._turnover_notional

    def apply_fill(self, fill: Fill) -> None:
        if not isinstance(fill, Fill):
            raise BacktestConfigurationError("Portfolio can apply only Fill values.")
        if fill.symbol != self._symbol:
            raise BacktestInvariantError(
                "Fill symbol does not match portfolio symbol."
            )
        if fill.fill_id in self._seen_fill_ids:
            raise BacktestInvariantError("The same fill cannot be applied twice.")
        if fill.order_id in self._seen_order_ids:
            raise BacktestInvariantError(
                "A target-position order cannot generate multiple fills in v1."
            )

        if fill.side is OrderSide.BUY:
            self._apply_buy(fill)
        else:
            self._apply_sell(fill)

        self._seen_fill_ids.add(fill.fill_id)
        self._seen_order_ids.add(fill.order_id)
        self._total_commission += fill.commission
        self._total_slippage += fill.slippage_cost
        self._turnover_notional += fill.notional
        self._assert_internal_invariants()

    def apply_corporate_action(
        self,
        event: CorporateActionEvent,
        *,
        policy: CorporateActionPolicy,
        dividend_tax_model: DividendTaxModel,
        reference_price: Decimal | None,
    ) -> CorporateActionApplication:
        if not isinstance(event, CorporateActionEvent):
            raise CorporateActionInvariantError(
                "Portfolio corporate actions require a CorporateActionEvent."
            )
        if not isinstance(policy, CorporateActionPolicy):
            raise CorporateActionInvariantError(
                "Portfolio corporate actions require a CorporateActionPolicy."
            )
        if event.event_id in self._seen_corporate_event_ids:
            raise CorporateActionInvariantError(
                "The same corporate-action event cannot be applied twice."
            )
        action = event.action
        if action.symbol != self._symbol:
            raise CorporateActionInvariantError(
                "Corporate-action symbol does not match the portfolio symbol."
            )

        before_symbol = self._symbol
        before_quantity = self._quantity
        before_basis = self._position_cost_basis
        cash_before = self._cash
        gross_amount = _ZERO
        tax_amount = _ZERO
        net_amount = _ZERO
        application_reference: Decimal | None = None

        if action.action_type in (
            CorporateActionType.SPLIT,
            CorporateActionType.REVERSE_SPLIT,
        ):
            gross_amount = self._apply_split(
                action,
                policy,
                event.event_at,
            )
            net_amount = gross_amount
            application_reference = action.cash_in_lieu_price
        elif event.phase is CorporateActionEventPhase.DIVIDEND_ENTITLEMENT:
            self._dividend_entitlements[action.action_id] = _DividendEntitlement(
                quantity=self._quantity,
                entry_at=self._open_entry_at,
            )
        elif event.phase is CorporateActionEventPhase.DIVIDEND_PAYMENT:
            gross_amount, tax_amount, net_amount = self._apply_dividend_payment(
                action,
                dividend_tax_model,
            )
        elif action.action_type is CorporateActionType.SYMBOL_CHANGE:
            assert action.new_symbol is not None
            self._symbol = action.new_symbol
        elif action.action_type is CorporateActionType.DELISTING:
            application_reference = self._apply_delisting(
                action,
                reference_price,
                event.event_at,
            )
            gross_amount = self._cash - cash_before
            net_amount = gross_amount
        else:
            raise CorporateActionInvariantError(
                "Unsupported corporate-action application."
            )

        self._seen_corporate_event_ids.add(event.event_id)
        self._corporate_action_count += 1
        self._assert_internal_invariants()
        application_id = str(uuid5(_APPLICATION_NAMESPACE, event.event_id))
        return CorporateActionApplication(
            application_id=application_id,
            event_id=event.event_id,
            action_id=action.action_id,
            action_type=action.action_type,
            phase=event.phase,
            applied_at=event.event_at.astimezone(UTC),
            symbol_before=before_symbol,
            symbol_after=self._symbol,
            quantity_before=before_quantity,
            quantity_after=self._quantity,
            cash_delta=self._cash - cash_before,
            cost_basis_before=before_basis,
            cost_basis_after=self._position_cost_basis,
            gross_amount=gross_amount,
            tax_amount=tax_amount,
            net_amount=net_amount,
            reference_price=application_reference,
        )

    def snapshot(
        self,
        *,
        timestamp: datetime,
        market_price: Decimal,
    ) -> PortfolioSnapshot:
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise BacktestConfigurationError(
                "Portfolio snapshot timestamp must be timezone-aware."
            )
        if (
            not isinstance(market_price, Decimal)
            or not market_price.is_finite()
            or market_price <= _ZERO
        ):
            raise BacktestConfigurationError(
                "Portfolio market price must be a finite positive Decimal."
            )
        market_value = market_price * self._quantity
        total_equity = self._cash + market_value
        if total_equity < _ZERO:
            raise BacktestInvariantError(
                "Long-only portfolio equity cannot be negative."
            )
        average_cost = (
            _ZERO
            if self._quantity == 0
            else self._position_cost_basis / self._quantity
        )
        unrealized_pnl = market_value - self._position_cost_basis
        exposure = _ZERO if total_equity == _ZERO else market_value / total_equity
        return PortfolioSnapshot(
            timestamp=timestamp.astimezone(UTC),
            symbol=self._symbol,
            cash=self._cash,
            quantity=self._quantity,
            position_cost_basis=self._position_cost_basis,
            average_cost=average_cost,
            market_price=market_price,
            market_value=market_value,
            total_equity=total_equity,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_commission=self._total_commission,
            total_slippage=self._total_slippage,
            exposure=exposure,
            total_dividend_gross=self._total_dividend_gross,
            total_dividend_tax=self._total_dividend_tax,
            total_dividend_net=self._total_dividend_net,
            total_cash_in_lieu=self._total_cash_in_lieu,
            corporate_action_count=self._corporate_action_count,
        )

    def _apply_buy(self, fill: Fill) -> None:
        total_cost = fill.notional + fill.commission
        if total_cost > self._cash:
            raise BacktestInvariantError("Buy fill exceeds available cash.")
        if self._quantity == 0:
            if self._open_entry_at is not None:
                raise BacktestInvariantError(
                    "Flat portfolio cannot retain an open-trade timestamp."
                )
            self._open_entry_at = fill.filled_at.astimezone(UTC)
            self._open_entry_cost = _ZERO
            self._open_exit_proceeds = _ZERO
            self._open_bought_quantity = 0
            self._open_sold_quantity = 0
        self._cash -= total_cost
        self._quantity += fill.quantity
        self._position_cost_basis += total_cost
        self._open_entry_cost += total_cost
        self._open_bought_quantity += fill.quantity

    def _apply_sell(self, fill: Fill) -> None:
        if fill.quantity > self._quantity:
            raise BacktestInvariantError("Sell fill exceeds the held quantity.")
        if self._open_entry_at is None:
            raise BacktestInvariantError("Sell fill requires an open long trade.")
        previous_quantity = self._quantity
        allocated_basis = (
            self._position_cost_basis * Decimal(fill.quantity) / previous_quantity
        )
        net_proceeds = fill.notional - fill.commission
        self._cash += net_proceeds
        self._quantity -= fill.quantity
        self._position_cost_basis -= allocated_basis
        self._realized_pnl += net_proceeds - allocated_basis
        self._open_exit_proceeds += net_proceeds
        self._open_sold_quantity += fill.quantity

        if self._quantity == 0:
            self._position_cost_basis = _ZERO
            self._finalize_trade(exit_at=fill.filled_at, identity=fill.fill_id)

    def _apply_split(
        self,
        action: CorporateActionRecord,
        policy: CorporateActionPolicy,
        event_at: datetime,
    ) -> Decimal:
        assert action.ratio_numerator is not None
        assert action.ratio_denominator is not None
        if self._quantity == 0:
            return _ZERO
        ratio = Decimal(action.ratio_numerator) / Decimal(action.ratio_denominator)
        exact_quantity = Decimal(self._quantity) * ratio
        whole_quantity = int(
            exact_quantity.to_integral_value(rounding=ROUND_FLOOR)
        )
        fractional_quantity = exact_quantity - whole_quantity
        cash_in_lieu = _ZERO
        if fractional_quantity != _ZERO:
            if policy.fractional_share_policy is FractionalSharePolicy.REJECT:
                raise CorporateActionEligibilityError(
                    "Split creates fractional shares under a reject policy."
                )
            if action.cash_in_lieu_price is None:
                raise CorporateActionEligibilityError(
                    "Cash-in-lieu policy requires an explicit cash price."
                )
            basis_per_new_share = self._position_cost_basis / exact_quantity
            fractional_basis = basis_per_new_share * fractional_quantity
            cash_in_lieu = action.cash_in_lieu_price * fractional_quantity
            self._cash += cash_in_lieu
            self._position_cost_basis -= fractional_basis
            self._realized_pnl += cash_in_lieu - fractional_basis
            self._total_cash_in_lieu += cash_in_lieu
            self._open_exit_proceeds += cash_in_lieu

        exact_sold = Decimal(self._open_sold_quantity) * ratio
        adjusted_sold = int(exact_sold.to_integral_value(rounding=ROUND_FLOOR))
        self._quantity = whole_quantity
        self._open_sold_quantity = adjusted_sold
        self._open_bought_quantity = adjusted_sold + whole_quantity
        if self._quantity == 0:
            self._position_cost_basis = _ZERO
            self._finalize_trade(
                exit_at=event_at,
                identity=action.action_id,
            )
        return cash_in_lieu

    def _apply_dividend_payment(
        self,
        action: CorporateActionRecord,
        dividend_tax_model: DividendTaxModel,
    ) -> tuple[Decimal, Decimal, Decimal]:
        entitlement = self._dividend_entitlements.pop(
            action.action_id,
            None,
        )
        if entitlement is None:
            raise CorporateActionInvariantError(
                "Dividend payment requires a prior entitlement event."
            )
        assert action.cash_amount_per_share is not None
        gross_amount = action.cash_amount_per_share * entitlement.quantity
        tax_amount = dividend_tax_model.calculate(gross_amount)
        if tax_amount < _ZERO or tax_amount > gross_amount:
            raise CorporateActionInvariantError(
                "Dividend tax model returned an invalid amount."
            )
        net_amount = gross_amount - tax_amount
        self._cash += net_amount
        self._realized_pnl += net_amount
        self._total_dividend_gross += gross_amount
        self._total_dividend_tax += tax_amount
        self._total_dividend_net += net_amount
        self._assign_dividend_to_trade(
            action=action,
            entitlement=entitlement,
            net_amount=net_amount,
        )
        return gross_amount, tax_amount, net_amount

    def _assign_dividend_to_trade(
        self,
        *,
        action: CorporateActionRecord,
        entitlement: _DividendEntitlement,
        net_amount: Decimal,
    ) -> None:
        if entitlement.quantity == 0:
            if entitlement.entry_at is not None:
                raise CorporateActionInvariantError(
                    "Zero-quantity dividend entitlement cannot reference a trade."
                )
            return
        if entitlement.entry_at is None:
            raise CorporateActionInvariantError(
                "Positive dividend entitlement must reference an open trade."
            )
        if self._open_entry_at == entitlement.entry_at:
            self._open_exit_proceeds += net_amount
            return
        assert action.ex_at is not None
        for index in range(len(self._closed_trades) - 1, -1, -1):
            trade = self._closed_trades[index]
            if trade.entry_at != entitlement.entry_at or trade.exit_at < action.ex_at:
                continue
            exit_proceeds = trade.exit_proceeds + net_amount
            net_pnl = exit_proceeds - trade.entry_cost
            self._closed_trades[index] = replace(
                trade,
                exit_proceeds=exit_proceeds,
                net_pnl=net_pnl,
                return_fraction=net_pnl / trade.entry_cost,
            )
            return
        raise CorporateActionInvariantError(
            "Dividend entitlement cannot be reconciled to an open or closed trade."
        )

    def _apply_delisting(
        self,
        action: CorporateActionRecord,
        reference_price: Decimal | None,
        exit_at: datetime,
    ) -> Decimal:
        if action.delisting_cash_price is not None:
            settlement_price = action.delisting_cash_price
        else:
            if (
                reference_price is None
                or not reference_price.is_finite()
                or reference_price < _ZERO
            ):
                raise CorporateActionEligibilityError(
                    "Recovery-rate delisting requires a nonnegative market price."
                )
            assert action.delisting_recovery_rate is not None
            settlement_price = reference_price * action.delisting_recovery_rate
        if self._quantity == 0:
            return settlement_price
        if self._open_entry_at is None:
            raise CorporateActionInvariantError(
                "A delisted position requires an open trade."
            )
        proceeds = settlement_price * self._quantity
        basis = self._position_cost_basis
        self._cash += proceeds
        self._realized_pnl += proceeds - basis
        self._open_exit_proceeds += proceeds
        self._open_sold_quantity += self._quantity
        self._quantity = 0
        self._position_cost_basis = _ZERO
        self._finalize_trade(exit_at=exit_at, identity=action.action_id)
        return settlement_price

    def _finalize_trade(self, *, exit_at: datetime, identity: str) -> None:
        entry_at = self._open_entry_at
        if entry_at is None:
            raise BacktestInvariantError("Trade finalization requires an open trade.")
        if self._open_bought_quantity != self._open_sold_quantity:
            raise BacktestInvariantError(
                "Closed trade quantities must balance after corporate actions."
            )
        trade_id = str(
            uuid5(
                _TRADE_NAMESPACE,
                "|".join(
                    (
                        self._symbol,
                        entry_at.isoformat(),
                        exit_at.astimezone(UTC).isoformat(),
                        identity,
                        str(len(self._closed_trades)),
                    )
                ),
            )
        )
        net_pnl = self._open_exit_proceeds - self._open_entry_cost
        self._closed_trades.append(
            ClosedTrade(
                trade_id=trade_id,
                symbol=self._symbol,
                entry_at=entry_at,
                exit_at=exit_at.astimezone(UTC),
                bought_quantity=self._open_bought_quantity,
                sold_quantity=self._open_sold_quantity,
                entry_cost=self._open_entry_cost,
                exit_proceeds=self._open_exit_proceeds,
                net_pnl=net_pnl,
                return_fraction=net_pnl / self._open_entry_cost,
            )
        )
        self._open_entry_at = None
        self._open_entry_cost = _ZERO
        self._open_exit_proceeds = _ZERO
        self._open_bought_quantity = 0
        self._open_sold_quantity = 0

    def _assert_internal_invariants(self) -> None:
        if self._cash < _ZERO:
            raise BacktestInvariantError(
                "Long-only portfolio cash cannot be negative."
            )
        if self._quantity < 0:
            raise BacktestInvariantError(
                "Long-only portfolio quantity cannot be negative."
            )
        if self._position_cost_basis < _ZERO:
            raise BacktestInvariantError("Position cost basis cannot be negative.")
        if self._quantity == 0 and self._position_cost_basis != _ZERO:
            raise BacktestInvariantError(
                "Flat portfolio cannot retain position cost basis."
            )
        if self._total_dividend_net != (
            self._total_dividend_gross - self._total_dividend_tax
        ):
            raise BacktestInvariantError(
                "Dividend accounting must reconcile gross, tax, and net amounts."
            )
        if self._open_entry_at is None:
            has_money = any(
                value != _ZERO
                for value in (self._open_entry_cost, self._open_exit_proceeds)
            )
            has_quantity = any(
                value != 0
                for value in (
                    self._open_bought_quantity,
                    self._open_sold_quantity,
                )
            )
            if has_money or has_quantity:
                raise BacktestInvariantError(
                    "Closed portfolio cannot retain open-trade accounting."
                )
        elif self._open_bought_quantity - self._open_sold_quantity != self._quantity:
            raise BacktestInvariantError(
                "Open-trade quantities must reconcile to the held quantity."
            )
