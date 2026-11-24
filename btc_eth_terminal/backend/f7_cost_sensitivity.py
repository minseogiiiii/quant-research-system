"""Fixed completed-path counterfactual fee/extra adverse price sensitivity.
Never reruns entry selection, alters liquidity, changes latency, or simulates
maker fills. Excluded failed/unresolved trades remain excluded and counted.
"""
from decimal import Decimal,localcontext
from f7_pnl_attribution import d,fmt,need,close

FEE_BPS=("0","0.5","1","2","5","10","20")
SLIP_BPS=("0","1","2","4")
BPS=Decimal(10000)

def economics(entry,exit,fee_bps,slip_bps):
    with localcontext() as ctx:
        ctx.prec=80
        f,s=d(fee_bps)/BPS,d(slip_bps)/BPS
        need(0<=f<1 and 0<=s<1 and entry>=0 and exit>=0,"SCENARIO_DOMAIN")
        buy=entry*(1+s);sell=exit*(1-s)
        fee=(buy+sell)*f
        extra=entry*s+exit*s
        return sell-buy-fee,fee,extra

def sensitivity(report,policy):
    need(report["schema"]=="f7_attribution_v1" and report["alpha_confirmed"] is False
         and report["execution_authorized"] is False and report["oos_approved"] is False,"SCENARIO_MODE")
    with localcontext() as ctx:
        ctx.prec=80
        t=report["totals"];entry=d(t["entry_depth_notional"]);exit=d(t["exit_depth_notional"])
        n=report["completed_roundtrips"]
        need(type(n) is int and n>=0 and (entry>0 and exit>0 if n else entry==exit==0),"SCENARIO_INVENTORY")
        baseline,fee,extra=economics(entry,exit,policy["fee_bps"],policy["extra_slippage_bps"])
        need(close(baseline,d(t["net_quote"])) and close(fee,d(t["fee_cost"])) and close(extra,d(t["extra_slippage_cost"])),"SCENARIO_BASELINE")
        rows=[];bounds=[]
        for s in SLIP_BPS:
            slip=d(s)/BPS;buy=entry*(1+slip);sell=exit*(1-slip)
            be=(sell-buy)/(sell+buy)*BPS if n else None
            bounds.append({"extra_slippage_bps":s,"break_even_fee_bps_signed":None if be is None else fmt(be),
                           "nonnegative_fee_can_break_even":None if be is None else be>=0})
            for f in FEE_BPS:
                net,fee,extra=economics(entry,exit,f,s)
                rows.append({"fee_bps":f,"extra_slippage_bps":s,"net_quote":fmt(net) if n else None,
                             "fee_quote":fmt(fee) if n else None,"extra_slippage_quote":fmt(extra) if n else None})
        return {"schema":"f7_fixed_path_cost_sensitivity_v1","completed_roundtrips":n,
                "unresolved_position":report["unresolved_position"],"baseline_reconciled":True,
                "scenario_rows":rows,"break_even":bounds,"scenario_selected":False,
                "latency_and_depth_path_fixed":True,"counterfactual_strategy_return":False,
                "actual_fee_or_fill_verified":False,"oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
