"""Synchronized receiver-grid sample covariance and causal fixed-intent sizing.

Simple mid returns per grid interval, not annualized or a forecast. Missing
paired marks reset the window; never forward fill or bridge source gaps.
"""

from collections import deque
from decimal import ROUND_FLOOR, Decimal, localcontext

from f4_depth_buckets import SCALE

PRODUCTS = ("BTC-USD", "ETH-USD")
POLICY = {
    "window_returns": 6,
    "risk_budget_fraction": "0.00002",
    "vol_floor": "0.0001",
    "max_notional_fraction": "0.05",
    "base_increment": "0.00000001",
}


def need(ok, why):
    if not ok:
        raise ValueError("H11C_" + why)


def validate_sizing(p):
    need(
        type(p) is dict
        and set(p) == set(POLICY)
        and type(p["window_returns"]) is int
        and 2 <= p["window_returns"] <= 120,
        "POLICY",
    )
    for k in set(POLICY) - {"window_returns"}:
        need(type(p[k]) is str and 0 < len(p[k]) <= 32, "VALUE")
        v = Decimal(p[k])
        need(v.is_finite() and 0 < v <= 1, "DOMAIN")
    inc = Decimal(p["base_increment"]) * SCALE
    need(inc == inc.to_integral_value() and inc >= 1, "INCREMENT")
    return p


def estimate(rows):
    with localcontext() as ctx:
        ctx.prec = 80
        n = len(rows)
        need(n >= 2, "WINDOW")
        means = [sum((x[i] for x in rows), Decimal(0)) / n for i in range(2)]
        a, b, c = [
            sum(((r[i] - means[i]) * (r[j] - means[j]) for r in rows), Decimal(0))
            / (n - 1)
            for i, j in ((0, 0), (0, 1), (1, 1))
        ]
        need(a >= 0 and c >= 0 and b * b <= a * c + Decimal("1e-70"), "PSD")
        va, vc = a.sqrt(), c.sqrt()
        corr = (
            None
            if not va or not vc
            else max(Decimal(-1), min(Decimal(1), b / (va * vc)))
        )
        return {
            "samples": n,
            "variance": {PRODUCTS[0]: a, PRODUCTS[1]: c},
            "covariance": b,
            "volatility": {PRODUCTS[0]: va, PRODUCTS[1]: vc},
            "correlation": corr,
        }


def serial(e):
    if e is None:
        return None
    return {
        k: (
            {p: format(v, "f") for p, v in x.items()}
            if isinstance(x, dict)
            else format(x, "f")
            if isinstance(x, Decimal)
            else x
        )
        for k, x in e.items()
    }


class RollingCovariance:
    def __init__(self, grid, window):
        need(
            type(grid) is int
            and 100 <= grid <= 60000
            and type(window) is int
            and 2 <= window <= 120,
            "CLOCK",
        )
        self.grid = grid
        self.window = window
        self.prev = None
        self.last = None
        self.returns = deque(maxlen=window)
        self.points = 0
        self.missing = 0
        self.ready = 0
        self.latest = None
        self.latest_time = None

    def observe(self, t, mids):
        need(
            type(t) is int and (self.last is None or t - self.last == self.grid), "GRID"
        )
        self.last = t
        self.points += 1
        if set(mids) != set(PRODUCTS):
            self.prev = None
            self.returns.clear()
            self.missing += 1
            return None
        x = tuple(mids[p] for p in PRODUCTS)
        need(all(type(v) is int and v > 0 for v in x), "MID")
        with localcontext() as ctx:
            ctx.prec = 80
            if self.prev is not None:
                self.returns.append(
                    tuple(
                        Decimal(v - old) / old
                        for v, old in zip(x, self.prev, strict=True)
                    )
                )
            self.prev = x
            if len(self.returns) < self.window:
                return None
            e = estimate(self.returns)
            self.ready += 1
            self.latest = e
            self.latest_time = t
            return e

    def receipt(self):
        return {
            "schema": "h11c_synchronized_covariance_v1",
            "grid_ms": self.grid,
            "window_returns": self.window,
            "grid_points": self.points,
            "missing_paired_marks": self.missing,
            "ready_estimates": self.ready,
            "last_ready_decision_ms": self.latest_time,
            "last_ready_estimate": serial(self.latest),
            "annualized": False,
            "forecast_validated": False,
            "missing_resets_window": True,
        }


def size_units(e, product, mid2, initial, maximum, p):
    # Freeze quantity at the signal decision, with no arrival-price lookahead.
    # Risk budget is standalone per-grid standard deviation in dollars.
    with localcontext() as ctx:
        ctx.prec = 80
        sigma = max(e["volatility"][product], Decimal(p["vol_floor"]))
        dollars = min(
            initial * Decimal(p["risk_budget_fraction"]) / sigma,
            initial * Decimal(p["max_notional_fraction"]),
        )
        mid = Decimal(mid2) / (2 * SCALE)
        step = int(Decimal(p["base_increment"]) * SCALE)
        raw = min(Decimal(maximum), dollars / mid * SCALE)
        return int((raw / step).to_integral_value(rounding=ROUND_FLOOR)) * step
