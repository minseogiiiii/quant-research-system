"""Offline development diagnostics on complete registered return matrices.

Uses Fincept's original pinned numerical modules; independent arithmetic
witnesses. No actual strategy fitting, no holdout access, no deployment.
"""

from __future__ import annotations
import argparse
import itertools
import json
import math
from pathlib import Path
from statistics import NormalDist, mean, stdev
import numpy as np
from f2_research_contract import regular_returns, psr_reference
from f6_fincept_metrics_adapter import load, VENDOR, PIN
from h5_experiment_registry import snapshot, series_hash, sha, need
from independent_soak_replay import strict_json


def modules():
    metrics = load()
    from base import robustness, monte_carlo

    for m in (metrics, robustness, monte_carlo):
        need(Path(m.__file__).resolve().is_relative_to(VENDOR), "VENDOR_MODULE")
    return metrics, robustness, monte_carlo


def same(a, b):
    return (
        a is not None
        and b is not None
        and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-10)
    )


def sr(v):
    return mean(v) / stdev(v)


def pbo_witness(matrix, blocks):
    t, n = matrix.shape
    edges = np.linspace(0, t, blocks + 1).astype(int)
    groups = [list(range(edges[i], edges[i + 1])) for i in range(blocks)]
    bad = loss = total = 0
    for subset in itertools.combinations(range(blocks), blocks // 2):
        test = set(range(blocks)) - set(subset)
        a = [i for j in subset for i in groups[j]]
        b = [i for j in sorted(test) for i in groups[j]]
        x = [sr(matrix[a, i].tolist()) for i in range(n)]
        y = [sr(matrix[b, i].tolist()) for i in range(n)]
        # Numerical ties in rank/winner can be ambiguous across algorithms.
        need(
            min(abs(v - w) for i, v in enumerate(x) for w in x[i + 1 :]) > 1e-10
            and min(abs(v - w) for i, v in enumerate(y) for w in y[i + 1 :]) > 1e-10,
            "PBO_NEAR_TIE",
        )
        best = max(range(n), key=x.__getitem__)
        rank = 1 + sum(v < y[best] for v in y)
        bad += int(2 * rank <= n + 1)
        loss += int(y[best] < 0)
        total += 1
    return bad / total, loss / total, total


def bootstrap(v, mc, seed, block):
    n = len(v)
    sims = 128
    horizon = min(n, 1000)
    sim = mc.simulate_paths(
        "block_bootstrap",
        v,
        n_sims=sims,
        seed=seed,
        initial=1.0,
        block_size=block,
        horizon=horizon,
    )
    # Same documented random draws, independent scalar index/path recurrence.
    rng = np.random.default_rng(seed)
    restart = rng.random((sims, horizon)) < 1 / block
    starts = rng.integers(0, n, size=(sims, horizon))
    finals = []
    dds = []
    for i in range(sims):
        idx = 0
        equity = peak = 1.0
        dd = 0.0
        for j in range(horizon):
            idx = int(starts[i, j]) if j == 0 or restart[i, j] else (idx + 1) % n
            equity *= 1 + float(v[idx])
            peak = max(peak, equity)
            dd = max(dd, 1 - equity / peak)
        finals.append(equity)
        dds.append(dd)
    need(
        np.allclose(sim["finals"], finals, rtol=1e-10, atol=1e-12)
        and np.allclose(sim["mdds"], dds, rtol=1e-10, atol=1e-12)
        and np.isfinite(sim["finals"]).all(),
        "BOOTSTRAP_PARITY",
    )
    return {
        "simulations": sims,
        "steps": horizon,
        "mean_block_bars": block,
        "seed": seed,
        "terminal_equity_p05_p50_p95": [
            float(x) for x in np.percentile(sim["finals"], [5, 50, 95])
        ],
        "max_drawdown_p50_p95": [
            float(x) for x in np.percentile(sim["mdds"], [50, 95])
        ],
        "scalar_path_parity": True,
        "stationarity_assumed_not_verified": True,
    }


def evaluate(journal, plan_pin, head, payload, *, fixture=False):
    reg = snapshot(journal, plan_pin, head)
    p = reg["plan"]
    need(
        reg["all_complete"]
        and p["source_kind"] == ("SYNTHETIC_TEST_ONLY" if fixture else "DEVELOPMENT"),
        "REGISTRY_INCOMPLETE_OR_MODE",
    )
    keys = {
        "schema",
        "return_basis",
        "timestamps",
        "interval_ms",
        "series",
        "split_policy",
        "source_kind",
    }
    need(
        type(payload) is dict
        and set(payload) == keys
        and payload["schema"] == "h5_registered_returns_v1"
        and payload["return_basis"] == "REGULAR_NET_PORTFOLIO_RETURNS"
        and payload["source_kind"] == p["source_kind"],
        "PAYLOAD",
    )
    timestamps = payload["timestamps"]
    interval = payload["interval_ms"]
    series = payload["series"]
    ids = [t["trial_id"] for t in p["trials"]]
    need(
        type(series) is dict
        and set(series) == set(ids)
        and 10 <= len(ids) <= 128
        and 200 <= len(timestamps) <= 10000
        and len(ids) * len(timestamps) <= 1000000,
        "MATRIX_INVENTORY",
    )
    for tid in ids:
        v = series[tid]
        need(
            type(v) is list
            and len(v) == len(timestamps)
            and all(
                type(x) in (float, int) and math.isfinite(x) and abs(x) <= 0.25
                for x in v
            ),
            "RETURN_VALUE",
        )
        need(
            series_hash(v, timestamps, interval)
            == reg["results"][tid]["return_series_sha256"],
            "SERIES_PIN",
        )
    r, ppy = regular_returns(list(zip(*(series[t] for t in ids), strict=True)), timestamps, interval)
    need(fixture or len(r) * interval >= 30 * 86400000, "INSUFFICIENT_DURATION")
    need(np.all(np.std(r, axis=0) > 1e-8), "DEGENERATE")
    sp = payload["split_policy"]
    need(
        type(sp) is dict
        and set(sp)
        == {
            "train_bars",
            "test_bars",
            "folds",
            "purge_bars",
            "embargo_bars",
            "information_lookback_bars",
            "label_delay_bars",
            "bootstrap_block_bars",
        }
        and sha(sp) == p["split_policy_sha256"]
        and all(type(x) is int for x in sp.values()),
        "SPLIT_PIN",
    )
    tr, te, k, purge, emb, look, delay, block = (
        sp[x]
        for x in (
            "train_bars",
            "test_bars",
            "folds",
            "purge_bars",
            "embargo_bars",
            "information_lookback_bars",
            "label_delay_bars",
            "bootstrap_block_bars",
        )
    )
    need(
        20 <= tr <= 10000
        and 20 <= te <= 10000
        and 2 <= k <= 10
        and 0 <= look <= purge <= 10000
        and 0 <= delay <= purge
        and purge >= look + delay
        and 0 <= emb <= 10000
        and 2 <= block <= min(100, len(r) // 4),
        "SPLIT_POLICY",
    )
    metrics, rob, mc = modules()
    wins = rob.walk_forward_windows(
        len(r),
        n_windows=k,
        train_bars=tr,
        test_bars=te,
        purge_bars=purge,
        embargo_bars=0,
        warmup_bars=0,
    )
    folds = []
    prior = []
    for i, w in enumerate(wins):
        start = len(r) - k * te + i * te
        end = start + te
        need(
            (w.test_start, w.test_end, w.train_start, w.train_end)
            == (start, end, start - purge - tr, start - purge),
            "WINDOW_PARITY",
        )
        # Independently intersect information intervals, including exclusions
        # after earlier test windows even if only part of the range overlaps.
        train = [
            j
            for j in range(w.train_start, w.train_end)
            if j >= look
            and j + delay < start - look
            and not any(j - look < b + emb and j + delay >= b for b in prior)
        ]
        test = [j for j in range(start, end) if j - look >= 0 and j + delay < end]
        need(len(train) >= 20 and len(test) >= 20, "FOLD_TOO_SHORT_AFTER_PURGE")
        need(max(train) + delay < min(test) - look, "FOLD_LEAKAGE")
        s = [metrics.sharpe_ratio(r[train, j], 0.0, 1.0) for j in range(len(ids))]
        witness = [sr(r[train, j].tolist()) for j in range(len(ids))]
        need(all(same(a, b) for a, b in zip(s, witness, strict=True)), "TRAIN_SCORE_PARITY")
        best = max(range(len(ids)), key=witness.__getitem__)
        need(best == max(range(len(ids)), key=s.__getitem__), "SELECTION_PARITY")
        out = metrics.total_return(
            np.concatenate(([1.0], np.cumprod(1 + r[test, best])))
        )
        need(
            same(out, math.prod(1 + float(x) for x in r[test, best]) - 1),
            "FOLD_RETURN_PARITY",
        )
        folds.append(
            {
                "fold": i,
                "selected_trial": ids[best],
                "train_rows": len(train),
                "test_rows": len(test),
                "train_last_index": max(train),
                "test_first_index": min(test),
                "test_last_index": max(test),
                "test_return": out,
                "train_sharpe_per_bar": s[best],
            }
        )
        prior.append(end)
    sharpes = np.array([sr(r[:, j].tolist()) for j in range(len(ids))])
    best = int(np.argmax(sharpes))
    ds = rob.deflated_sharpe(r[:, best], sharpes, len(ids))
    v = r[:, best].tolist()
    mu = mean(v)
    m2 = mean((x - mu) ** 2 for x in v)
    skew = mean((x - mu) ** 3 for x in v) / m2**1.5
    kurt = mean((x - mu) ** 4 for x in v) / m2**2
    n = len(ids)
    var = stdev(sharpes.tolist()) ** 2
    expected = math.sqrt(var) * (
        (1 - 0.5772156649015329) * NormalDist().inv_cdf(1 - 1 / n)
        + 0.5772156649015329 * NormalDist().inv_cdf(1 - 1 / (n * math.e))
    )
    ds_ref = psr_reference(sharpes[best], expected, len(v), skew, kurt)
    need(
        same(ds["expectedMaxSharpePerBar"], expected)
        and same(ds["deflatedSharpe"], ds_ref),
        "DSR_PARITY",
    )
    # A small bounded CSCV comparison; block-local variation is mandatory.
    blocks = 4
    edges = np.linspace(0, len(r), blocks + 1).astype(int)
    need(
        all(np.all(np.std(r[a:b], axis=0) > 1e-8) for a, b in zip(edges[:-1], edges[1:], strict=True)),
        "PBO_BLOCK_VARIANCE",
    )
    pb = rob.pbo_cscv(r, n_blocks=blocks)
    pw, loss, ns = pbo_witness(r, blocks)
    need(
        same(pb["pbo"], pw) and same(pb["probOosLoss"], loss) and pb["splits"] == ns,
        "PBO_PARITY",
    )
    return {
        "schema": "h5_fincept_robustness_v1",
        "vendor_commit": PIN,
        "plan_sha256": plan_pin,
        "registry_head_sha256": head,
        "source_kind": p["source_kind"],
        "trials": n,
        "observations": len(r),
        "periods_per_year": ppy,
        "folds": folds,
        "walk_forward_scope": (
            "PRECOMPUTED_FIXED_RETURN_DEVELOPMENT_DIAGNOSTIC_NO_REFIT_NO_STITCH"
        ),
        "development_best_trial": ids[best],
        "dsr": ds,
        "pbo": pb,
        "monte_carlo": bootstrap(r[:, best], mc, p["seed"], block),
        "witness_parity": True,
        "trial_inventory_complete_within_plan": True,
        "all_prior_research_trials_verified": False,
        "information_interval_bounds_declared_not_independently_verified": True,
        "oos_approved": False,
        "alpha_confirmed": False,
        "execution_authorized": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    p.add_argument("payload", type=Path)
    p.add_argument("--plan-sha256", required=True)
    p.add_argument("--head-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    need(a.payload.stat().st_size <= 40_000_000, "PAYLOAD_SIZE")
    r = evaluate(
        a.journal, a.plan_sha256, a.head_sha256, strict_json(a.payload.read_bytes())
    )
    with a.output.open("x") as f:
        json.dump(r, f, sort_keys=True, allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
