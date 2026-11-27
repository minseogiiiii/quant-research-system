"""Causal indicative joint dollar volatility admission, not a loss guarantee."""

from decimal import Decimal, localcontext

from f4_depth_buckets import PRODUCTS, SCALE


def need(ok, why):
    if not ok:
        raise ValueError("H12D_" + why)


def validate_budget(v):
    need(type(v) is str and 0 < len(v) <= 32, "BUDGET")
    x = Decimal(v)
    need(x.is_finite() and 0 < x <= 1, "DOMAIN")
    return x


def dollar_vol(e, exposures):
    with localcontext() as ctx:
        ctx.prec = 80
        x, y = (exposures.get(p, Decimal(0)) for p in PRODUCTS)
        a, c = (e["variance"][p] for p in PRODUCTS)
        b = e["covariance"]
        need(
            x >= 0
            and y >= 0
            and a >= 0
            and c >= 0
            and b * b <= a * c + Decimal("1e-70"),
            "DOMAIN",
        )
        v = x * x * a + 2 * x * y * b + y * y * c
        need(v >= -Decimal("1e-55"), "VARIANCE")
        return max(v, Decimal(0)).sqrt()


class JointRisk:
    def __init__(self, initial, budget):
        self.cap = initial * validate_budget(budget)
        self.estimate = None
        self.stamp = None
        self.grid = None
        self.count = {}
        self.checked = 0
        self.passed = 0
        self.max_admitted = Decimal(0)

    def update(self, t, e, grid):
        need(self.stamp is None or t - self.stamp == grid, "GRID")
        self.stamp = t
        self.estimate = e
        self.grid = grid

    def admit(self, t, product, q, pos, books, clocks, age):
        self.checked += 1
        reason = None
        if (
            self.estimate is None
            or self.stamp is None
            or not 0 < t - self.stamp <= self.grid
        ):
            reason = "joint_covariance_unavailable"
        else:
            e = self.estimate
            # No zero/near singular covariance reliance for risk admission.
            a, c = (e["variance"][p] for p in PRODUCTS)
            b = e["covariance"]
            if min(a, c) <= 0 or a * c - b * b <= a * c * Decimal("0.00000001"):
                reason = "joint_covariance_degenerate"
        if reason is None:
            exposures = {}
            for p in PRODUCTS:
                stamp, _ = clocks[p]
                book = books[p]
                if (
                    stamp is None
                    or not 0 <= t - stamp <= age
                    or not book.bid
                    or not book.offer
                ):
                    reason = "joint_stale_mark"
                    break
                quantity = q if p == product else pos.get(p, (None, Decimal(0)))[1]
                exposures[p] = (
                    Decimal(book.best("bid")[0] + book.best("offer")[0])
                    * quantity
                    / (2 * SCALE)
                )
            if reason is None:
                v = dollar_vol(e, exposures)
                if v > self.cap:
                    reason = "joint_volatility_cap"
                else:
                    self.passed += 1
                    self.max_admitted = max(self.max_admitted, v)
        if reason:
            self.count[reason] = self.count.get(reason, 0) + 1
        return reason

    def receipt(self):
        need(self.checked == self.passed + sum(self.count.values()), "COUNTS")
        return {
            "schema": "h12_joint_entry_risk_v1",
            "dollar_volatility_budget": format(self.cap, "f"),
            "checks": self.checked,
            "passed_checks": self.passed,
            "rejections": self.count,
            "largest_passing_projected_dollar_volatility": format(
                self.max_admitted, "f"
            ),
            "entry_only": True,
            "forecast_validated": False,
            "risk_limit_guaranteed": False,
        }
