"""Additive completed-roundtrip attribution and regular-grid equity witness.

Trace is emitted by the existing frozen strategy; no change to signal or fill
selection. All cashflows are hypothetical. No OOS or actual-fill claims.
"""

from decimal import Decimal, localcontext
from f6_liquidity_recovery import project, digest

COSTS = ("spread_cost", "depth_cost", "extra_slippage_cost", "fee_cost")


def need(ok, why):
    if not ok:
        raise ValueError("F7_" + why)


def d(v):
    x = Decimal(v)
    need(x.is_finite(), "NONFINITE")
    return x


def fmt(v):
    return format(v, "f")


def close(a, b):
    return abs(a - b) <= Decimal("1e-65") * max(Decimal(1), abs(a), abs(b))


def decompose(buy, sell, signal_mid, exit_mid=None):
    with localcontext() as ctx:
        ctx.prec = 80
        b, s = buy["quote"], sell["quote"]
        q = d(b["requested_base"])
        need(q > 0 and q == d(s["requested_base"]), "QTY")
        bm, sm = d(b["mid"]), d(s["mid"])
        bt, st = d(b["touch"]), d(s["touch"])
        bg, sg = d(b["gross_quote"]), d(s["gross_quote"])
        ba, sa = d(b["adverse_adjusted_quote"]), d(s["adverse_adjusted_quote"])
        bf, sf = d(b["assumed_fee_quote"]), d(s["assumed_fee_quote"])
        bn, sn = d(b["all_in_quote"]), d(s["all_in_quote"])
        need(close(bn, ba + bf) and close(sn, sa - sf), "CASHFLOW")
        gross = (sm - bm) * q
        costs = {
            "spread_cost": (bt - bm + sm - st) * q,
            "depth_cost": bg - bt * q + st * q - sg,
            "extra_slippage_cost": ba - bg + sg - sa,
            "fee_cost": bf + sf,
        }
        need(all(v >= -Decimal("1e-65") for v in costs.values()), "NEGATIVE_COST")
        net = sn - bn
        need(close(gross - sum(costs.values()), net), "DECOMPOSITION")
        out = {
            "arrival_mid_gross_quote": gross,
            **costs,
            "net_quote": net,
            "entry_depth_notional": bg,
            "exit_depth_notional": sg,
            "entry_latency_pnl": (d(signal_mid) - bm) * q,
        }
        # Holding gross at decision clocks and signed latency components are
        # only available when an earlier, fresh exit-decision quote exists.
        if exit_mid is not None:
            em = d(exit_mid)
            out.update(
                decision_mid_gross_quote=(em - d(signal_mid)) * q,
                exit_latency_pnl=(sm - em) * q,
            )
            need(
                close(
                    out["decision_mid_gross_quote"]
                    + out["entry_latency_pnl"]
                    + out["exit_latency_pnl"],
                    gross,
                ),
                "LATENCY_PARITY",
            )
        else:
            out.update(decision_mid_gross_quote=None, exit_latency_pnl=None)
        return {k: None if v is None else fmt(v) for k, v in out.items()}


def summarize(packets, product, policy=None):
    trace = []
    report = project(packets, product, policy, trace=trace)
    with localcontext() as ctx:
        ctx.prec = 80
        signal = buy = exit_mid = None
        completed = []
        grid = []
        cash = d(report["baseline_cash"])
        base = Decimal(0)
        for row in trace:
            k = row["kind"]
            if k == "SIGNAL":
                need(buy is None, "OVERLAP")
                signal = row
            elif k == "BUY":
                need(
                    signal is not None
                    and buy is None
                    and signal["time"] == row["decision_ms"] < row["time"],
                    "BUY_TIME",
                )
                buy = row
                cash -= d(row["quote"]["all_in_quote"])
                base = d(row["quote"]["requested_base"])
                exit_mid = None
            elif k == "EXIT_DECISION":
                need(buy is not None and row["time"] > buy["time"], "EXIT_TIME")
                exit_mid = row["mid"]
            elif k == "SELL":
                need(buy is not None and row["time"] > buy["time"], "SELL_TIME")
                completed.append(decompose(buy, row, signal["mid"], exit_mid))
                cash += d(row["quote"]["all_in_quote"])
                base = Decimal(0)
                buy = signal = exit_mid = None
            elif k == "REJECT":
                if row["side"] == "BUY":
                    signal = None
            elif k == "GRID":
                need(
                    close(cash, d(row["cash"])) and close(base, d(row["base"])),
                    "LEDGER_CASH_BASE",
                )
                equity = (
                    cash + base * d(row["mid"])
                    if base and row["mid"] is not None
                    else cash
                    if not base
                    else None
                )
                grid.append(
                    {
                        "time": row["time"],
                        "equity": fmt(equity) if equity is not None else None,
                        "base": fmt(base),
                    }
                )
            else:
                raise ValueError("F7_TRACE_KIND")
        need(
            len(completed) == report["closed_roundtrips"]
            and bool(base) == report["unresolved_position"],
            "INVENTORY",
        )
        fields = (
            "arrival_mid_gross_quote",
            *COSTS,
            "net_quote",
            "entry_latency_pnl",
            "entry_depth_notional",
            "exit_depth_notional",
        )
        totals = {k: fmt(sum((d(x[k]) for x in completed), Decimal(0))) for k in fields}
        need(
            close(d(totals["net_quote"]), d(report["realized_net_quote"])),
            "NET_RECONCILIATION",
        )
        latency_complete = sum(
            x["decision_mid_gross_quote"] is not None for x in completed
        )
        if latency_complete == len(completed):
            for k in ("decision_mid_gross_quote", "exit_latency_pnl"):
                totals[k] = fmt(sum((d(x[k]) for x in completed), Decimal(0)))
        else:
            totals.update(decision_mid_gross_quote=None, exit_latency_pnl=None)
        interval = report["policy"]["step_ms"]
        need(
            all(b["time"] - a["time"] == interval for a, b in zip(grid, grid[1:])),
            "GRID_GAP",
        )
        complete = bool(grid) and all(
            x["equity"] is not None and d(x["equity"]) > 0 for x in grid
        )
        final_reconciled = (
            complete
            and not report["unresolved_position"]
            and d(grid[-1]["base"]) == 0
            and close(d(grid[-1]["equity"]), cash)
        )
        returns = []
        times = []
        if complete and len(grid) >= 3:
            for a, b in zip(grid, grid[1:]):
                returns.append(float(d(b["equity"]) / d(a["equity"]) - 1))
                times.append(b["time"])
        return report, {
            "schema": "f7_attribution_v1",
            "strategy_report_sha256": digest(report),
            "product": product,
            "policy_sha256": report["policy_sha256"],
            "completed_roundtrips": len(completed),
            "latency_complete_roundtrips": latency_complete,
            "totals": totals,
            "unresolved_position": report["unresolved_position"],
            "grid_points": len(grid),
            "missing_equity_points": sum(x["equity"] is None for x in grid),
            "complete_regular_grid": complete,
            "final_flat_cash_reconciled": final_reconciled,
            "grid_start_ms": grid[0]["time"] if grid else None,
            "grid_end_ms": grid[-1]["time"] if grid else None,
            "interval_ms": interval,
            "returns": returns,
            "timestamps": times,
            "actual_fills_verified": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
