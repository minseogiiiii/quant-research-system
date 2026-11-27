"""Opt-in offline Fincept metrics runner; no live or trading dependency.

Requires explicit regular net portfolio returns, not trade/event returns. Vendor
license remains AGPL-3.0; combined deployment is not authorized by this module.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from statistics import mean, stdev
import numpy as np
from f2_research_contract import regular_returns

VENDOR = Path(__file__).resolve().parents[1] / "vendor/fincept"
PIN = "914134de5fd227fe66969c4591ac9ca7e461cdf4"


def need(ok, msg):
    if not ok:
        raise ValueError("F6_" + msg)


def load():
    inventory = json.loads((VENDOR / "UPSTREAM.json").read_text())
    need(
        inventory["commit"] == PIN and inventory["license"] == "AGPL-3.0", "VENDOR_PIN"
    )
    root = VENDOR.parents[2]
    for e in inventory["files"]:
        path = root / e["local"]
        raw = path.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        need(blob == e["git_blob"], "VENDOR_BLOB")
    sys.path.insert(0, str(VENDOR))
    from base import metrics

    need(Path(metrics.__file__).resolve().is_relative_to(VENDOR), "VENDOR_IMPORT")
    return metrics


def evaluate(returns, timestamps, interval_ms):
    r, ppy = regular_returns(returns, timestamps, interval_ms)
    need(r.ndim == 1, "RETURN_SERIES")
    m = load()
    eq = np.concatenate(([1.0], np.cumprod(1 + r)))
    need(np.isfinite(eq).all() and (eq > 0).all(), "EQUITY_DOMAIN")
    vals = r.tolist()
    n = len(vals)
    peak = 1.0
    drawdown = 0.0
    equity = 1.0
    for v in vals:
        equity *= 1 + v
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    total = m.total_return(eq)
    dd = m.max_drawdown(eq)
    need(
        math.isclose(total, equity - 1, rel_tol=1e-10, abs_tol=1e-12)
        and math.isclose(dd, drawdown, rel_tol=1e-10, abs_tol=1e-12),
        "METRIC_PARITY",
    )
    sr = m.sharpe_ratio(r, 0.0, ppy) if n >= 20 else None
    sd = stdev(vals)
    if n >= 20 and sd > 1e-10 and sr is not None:
        need(
            math.isclose(
                sr, mean(vals) / sd * math.sqrt(ppy), rel_tol=1e-9, abs_tol=1e-9
            ),
            "SHARPE_PARITY",
        )
    # Explicitly return-tail quantile, not positive loss magnitude.
    var = m.value_at_risk(r) if n >= 20 else None
    cvar = m.conditional_var(r) if n >= 20 else None
    if var is not None:
        sorted_r = sorted(vals)
        q = 0.05 * (n - 1)
        i = int(q)
        reference = sorted_r[i] + (q - i) * (sorted_r[min(i + 1, n - 1)] - sorted_r[i])
        need(math.isclose(var, reference, rel_tol=1e-10, abs_tol=1e-12), "VAR_PARITY")
        tail = [v for v in vals if v <= var]
        need(
            tail and math.isclose(cvar, mean(tail), rel_tol=1e-10, abs_tol=1e-12),
            "CVAR_PARITY",
        )
    return {
        "schema": "f6_fincept_metrics_v1",
        "vendor_commit": PIN,
        "observations": n,
        "periods_per_year": ppy,
        "total_return": total,
        "max_drawdown": dd,
        "annualized_sample_sharpe": sr,
        "return_var_95": var,
        "return_cvar_95": cvar,
        "short_sample_warning": n * interval_ms < 30 * 86400000,
        "metric_parity": True,
        "risk_free_per_bar": 0.0,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    v = json.loads(a.input.read_text())
    need(v.get("return_basis") == "REGULAR_NET_PORTFOLIO_RETURNS", "RETURN_BASIS")
    result = evaluate(v["returns"], v["timestamps"], v["interval_ms"])
    with a.output.open("x") as f:
        json.dump(result, f, sort_keys=True, allow_nan=False)


if __name__ == "__main__":
    main()
