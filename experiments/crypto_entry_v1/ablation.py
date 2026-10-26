"""Point-in-time group ablation: G1 trend; G2 volume/profile; G3 volatility; G4 taker flow.

Research-only SHORT labels from spot returns do not establish that an actual
short instrument can be traded at these reference prices or costs.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.metrics import mean_squared_error
from history import read_months

# No Book data is manufactured from 1m bars. G5/G6 require collected L2 events.
GROUPS = {
    "G1_price_trend": ["ret_5", "ret_15", "ret_60", "ema_gap", "range_position", "body_fraction"],
    "G2_volume_profile": ["rel_volume", "vwap_gap", "volume_chg", "poc_gap"],
    "G3_volatility": ["rv_30", "rv_120", "atr_30", "rv_ratio", "shock"],
    "G4_aggressive_flow": ["taker_imb_5", "taker_imb_30", "flow_5", "flow_30", "buy_persistence"],
}


def _profile_poc(close: np.ndarray, qvolume: np.ndarray, indices: np.ndarray, window: int = 120) -> np.ndarray:
    """Trailing volume-profile POC estimator computed only at decision timestamps."""
    out = np.full(len(indices), np.nan)
    for k, i in enumerate(indices):
        if i < window - 1:
            continue
        prices = close[i-window+1:i+1]
        weights = qvolume[i-window+1:i+1]
        if not np.isfinite(prices).all() or prices.min() <= 0:
            continue
        log_p = np.log(prices)
        if log_p.min() == log_p.max():
            out[k] = float(np.exp(log_p[0]))
        else:
            hist, edges = np.histogram(log_p, bins=24, weights=weights)
            j = int(np.argmax(hist))
            out[k] = float(np.exp((edges[j] + edges[j+1]) / 2))
    return out


def build_features(df: pd.DataFrame, horizon: int = 30) -> pd.DataFrame:
    """Compute features at fully closed minute t; model entry = open[t+2] (one full minute delay).

    Fixed horizon 30 minutes uses exit proxy = open[t+2+horizon].
    All rolling features use indices <= t; targets use strictly later opens.
    """
    v = df.copy()
    for c in ["open", "high", "low", "close", "quote_volume", "taker_buy_quote", "volume"]:
        v[c] = v[c].astype(float)
    logp = np.log(v.close)
    ret = logp.diff()
    eps = 1e-12
    for n in [5, 15, 60]:
        v[f"ret_{n}"] = logp.diff(n)
    ema_fast = v.close.ewm(span=12, adjust=False).mean()
    ema_slow = v.close.ewm(span=60, adjust=False).mean()
    v["ema_gap"] = (ema_fast / ema_slow - 1)
    hi = v.high.rolling(60, min_periods=60).max()
    lo = v.low.rolling(60, min_periods=60).min()
    v["range_position"] = ((v.close-lo)/(hi-lo+eps)-0.5) * 2
    v["body_fraction"] = (v.close-v.open)/(v.high-v.low+eps)
    v["rel_volume"] = (v.quote_volume.rolling(5).sum() /
                        (v.quote_volume.shift(5).rolling(60).mean()*5+eps))
    v["volume_chg"] = np.log1p(v.quote_volume.rolling(5).sum()) - np.log1p(v.quote_volume.shift(5).rolling(5).sum())
    vwap_60 = (v.close * v.quote_volume).rolling(60).sum() / (v.quote_volume.rolling(60).sum()+eps)
    v["vwap_gap"] = v.close / vwap_60 - 1
    for n in (30,120):
        v[f"rv_{n}"] = ret.rolling(n).std() * np.sqrt(30)
    prev_c = v.close.shift(1)
    tr = pd.concat([(v.high-v.low), (v.high-prev_c).abs(),(v.low-prev_c).abs()], axis=1).max(axis=1)
    v["atr_30"] = tr.rolling(30).mean()/v.close
    v["rv_ratio"] = v.rv_30 / (v.rv_120+eps)
    v["shock"] = ret.rolling(5).sum()/(ret.rolling(120).std()*np.sqrt(5)+eps)
    q = v.quote_volume
    signed = (2*v.taker_buy_quote - q).where(q>0, 0)
    for n in (5,30):
        v[f"taker_imb_{n}"] = signed.rolling(n).sum()/(q.rolling(n).sum()+eps)
        v[f"flow_{n}"] = signed.rolling(n).sum()/(q.shift(n).rolling(120).mean()*n+eps)
    v["buy_persistence"] = (signed > 0).rolling(15).mean()
    # Only every five MINUTES is a decision; the ts stamp denotes candle OPEN.
    # Signals are available at the end of that 1m candle, not ts itself.
    indices = np.flatnonzero((v.ts.dt.minute % 5 == 4).to_numpy())
    poc = _profile_poc(v.close.to_numpy(), v.quote_volume.to_numpy(), indices)
    v["poc_gap"] = np.nan
    v.loc[indices, "poc_gap"] = v.close.iloc[indices].to_numpy() / poc - 1
    v["future_return"] = np.log(v.open.shift(-(2+horizon)) / v.open.shift(-2))
    v["decision_time_utc"] = v.ts + pd.Timedelta(minutes=1)
    feature_names = [x for arr in GROUPS.values() for x in arr]
    v = v.iloc[indices][["decision_time_utc", "future_return", *feature_names]].copy()
    v = v.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    if len(v) == 0:
        raise ValueError("No eligible decision rows after feature/label filtering")
    return v


def select_split(df: pd.DataFrame, train_end: str, val_end: str, test_end: str):
    dates = pd.to_datetime(df.decision_time_utc, utc=True)
    a, b, c = [pd.Timestamp(x, tz="UTC") for x in [train_end, val_end, test_end]]
    if not a < b < c:
        raise ValueError("Must have train_end < val_end < test_end")
    # Embargo 35m > 30m target overlap, applied before every split boundary.
    embargo = pd.Timedelta(minutes=35)
    tr = df.loc[dates < a-embargo]
    va = df.loc[(dates >= a+embargo) & (dates < b-embargo)]
    te = df.loc[(dates >= b+embargo) & (dates < c)]
    for label, part in (("training",tr),("validation",va),("holdout",te)):
        if len(part) < 100:
            raise ValueError(f"Insufficient {label} decisions: {len(part)}; require >=100")
    return tr, va, te


def pick_threshold(p_val: np.ndarray, y_val: np.ndarray, fee_bps: float) -> float:
    """Select a no-trade buffer on validation ONLY; enforce minimum participation."""
    fee = fee_bps/10000
    best = (float("-inf"), 0.002)
    for extra in [0.0,0.0002,0.0005,0.001,0.002]:
        pred_action = np.where(p_val > fee + extra, 1, np.where(p_val < -(fee+extra), -1, 0))
        traded = pred_action != 0
        if traded.sum() < 30:
            continue
        pnl = pred_action*y_val - traded*fee
        # per-decision utility: WAIT=0, avoids picking a 1-trade marvel
        utility = float(pnl.mean())
        if utility > best[0]:
            best = (utility, extra)
    return best[1]


def evaluate(pred: np.ndarray, y: np.ndarray, fee_bps: float, extra: float) -> dict:
    fee = fee_bps/10000
    action = np.where(pred > fee+extra, 1, np.where(pred < -(fee+extra), -1, 0))
    traded = action != 0
    pnl = action * y - traded * fee
    trades = pnl[traded]
    return {"mse": float(mean_squared_error(y,pred)),
            "mean_pnl_bp_per_decision": float(pnl.mean()*10000),
            "mean_pnl_bp_per_trade": float(trades.mean()*10000) if len(trades) else None,
            "trades": int(traded.sum()),"coverage": float(traded.mean()),
            "long_count": int((action==1).sum()),"short_count":int((action==-1).sum())}


def fit_predict(train: pd.DataFrame, val: pd.DataFrame, test: pd.DataFrame, features: list[str]):
    # Strictly fit scaler & model only on training data.
    model = make_pipeline(StandardScaler(), Ridge(alpha=100.0))
    model.fit(train[features], train.future_return)
    return model.predict(val[features]), model.predict(test[features])


def paired_daily_ci(a: np.ndarray, b: np.ndarray, dates: pd.Series, repeats: int = 500) -> list[float]:
    """Paired difference a-b; bootstrap UTC days to preserve within-day dependence."""
    day = pd.to_datetime(dates, utc=True).dt.floor("D").to_numpy()
    unique, inv = np.unique(day, return_inverse=True)
    totals = np.bincount(inv, weights=a-b)
    counts = np.bincount(inv)
    rng = np.random.default_rng(20261025)
    indices = rng.integers(0, len(unique), (repeats, len(unique)))
    means = totals[indices].sum(axis=1) / counts[indices].sum(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def _decision_pnl(pred: np.ndarray, actual: np.ndarray, bps: float, extra: float) -> np.ndarray:
    cost = bps/10000
    a = np.where(pred > cost+extra, 1, np.where(pred < -(cost+extra), -1, 0))
    return a*actual-(a != 0)*cost


def run_ablation(df: pd.DataFrame, train_end: str, val_end: str, test_end: str,
                 fee_bps: float = 20) -> dict:
    tr,va,te = select_split(df,train_end,val_end,test_end)
    all_features=[f for grp in GROUPS.values() for f in grp]
    configs={"BASE_MEAN":[],"FULL_G1_G4":all_features}
    configs.update({f"ONLY_{k}":fs for k,fs in GROUPS.items()})
    configs.update({f"DROP_{k}":[x for x in all_features if x not in fs] for k,fs in GROUPS.items()})
    result={"status":"EMPIRICAL_IF_RAW_ARCHIVE_VERIFIED","protocol":{
         "train_end":train_end,"validation_end":val_end,"test_end":test_end,
         "decision_interval":"5m","horizon":"30m","execution_proxy":"open[t+2] to open[t+32] (one full minute execution delay)",
         "embargo_minutes":35,"fee_bps_round_trip_per_action":fee_bps,
         "short_warning":"Spot proxy for short, NOT tradable short PnL; no funding/borrow/liquidation model",
         "model":"StandardScaler + Ridge(alpha=100), no holdout fitting",
         "threshold":"chosen only on validation", "holdout_usage":"ONE_TIME"},
         "rows":{"train":len(tr),"validation":len(va),"holdout":len(te)},"models":{}}
    holdout_predictions: dict[str,tuple[np.ndarray,float]] = {}
    for name, fs in configs.items():
        if fs:
            p_val,p_test=fit_predict(tr,va,te,fs)
        else:
            mean=float(tr.future_return.mean())
            p_val=np.full(len(va),mean)
            p_test=np.full(len(te),mean)
        buffer=pick_threshold(p_val,va.future_return.to_numpy(),fee_bps)
        holdout_predictions[name]=(p_test,buffer)
        result["models"][name]={"selected_buffer_bp":buffer*10000,
            "validation":evaluate(p_val,va.future_return.to_numpy(),fee_bps,buffer),
            "holdout":evaluate(p_test,te.future_return.to_numpy(),fee_bps,buffer)}
    full=result["models"]["FULL_G1_G4"]["holdout"]
    for k in GROUPS:
        drop=result["models"][f"DROP_{k}"]["holdout"]
        result["models"][f"DROP_{k}"]["full_minus_drop_delta_mse"]=(drop["mse"]-full["mse"])
        result["models"][f"DROP_{k}"]["full_minus_drop_delta_pnl_bp_per_decision"]=(full["mean_pnl_bp_per_decision"]-drop["mean_pnl_bp_per_decision"])
        yy=te.future_return.to_numpy()
        pf,bf=holdout_predictions["FULL_G1_G4"]
        pd_,bd=holdout_predictions[f"DROP_{k}"]
        # Positive difference favors FULL; 95% day-block confidence intervals.
        ci_mse=paired_daily_ci((yy-pd_)**2,(yy-pf)**2,te.decision_time_utc)
        ci_pnl=paired_daily_ci(_decision_pnl(pf,yy,fee_bps,bf),_decision_pnl(pd_,yy,fee_bps,bd),te.decision_time_utc)
        result["models"][f"DROP_{k}"]["full_minus_drop_mse_95ci"] = ci_mse
        result["models"][f"DROP_{k}"]["full_minus_drop_pnl_bp_per_decision_95ci"] = [x*1e4 for x in ci_pnl]
    # Keep economic utility and MSE separately; missing execution realism caveat.
    return result


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--archive-dir",type=Path,required=True)
    ap.add_argument("--train-end",default="2025-07-01")
    ap.add_argument("--val-end",default="2026-01-01")
    ap.add_argument("--test-end",default="2026-10-01")
    ap.add_argument("--roundtrip-bps",type=float,default=20.0)
    ap.add_argument("--output",type=Path,default=Path("results/ablation.json"))
    args=ap.parse_args()
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite a consumed holdout result; use a new versioned output path")
    df=build_features(read_months(args.archive_dir))
    res=run_ablation(df,args.train_end,args.val_end,args.test_end,args.roundtrip_bps)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(res,indent=2,allow_nan=False))
    print(json.dumps({"rows":res["rows"],"models":res["models"]},indent=2))

if __name__ == "__main__":
    main()