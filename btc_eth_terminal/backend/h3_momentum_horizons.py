"""Frozen long-only momentum opportunities vs nested microstructure filter.

Overlapping opportunities are independent hypothetical quote paths, NOT a
portfolio backtest or independent statistical samples. No parameter selection.
"""
from __future__ import annotations
from collections import deque
from decimal import Decimal,localcontext
import heapq
from f4_depth_buckets import units,SCALE,PRODUCTS
from f4_ofi_markout import ofi
from f5_execution_depth import walk
from f6_liquidity_recovery import digest
from post_quote_markout import IndexedBook

POLICY={"grid_ms":10000,"momentum_ms":60000,"momentum_bps":"2","ofi_ms":5000,
        "imbalance_min":"0.1","max_age_ms":3000,"latency_ms":100,"tolerance_ms":1500,
        "horizons_ms":[10000,30000,60000,300000,900000],
        "quantity":{"BTC-USD":"0.01","ETH-USD":"0.1"},"capacity_fraction":"0.5",
        "fee_bps":"10","extra_slippage_bps":"2"}
def need(ok,why):
    if not ok:raise ValueError("H3_"+why)
def fmt(x):return format(x,"f")

def project(packets,product,policy=None,*,signal_sink=None):
    import json
    p=json.loads(json.dumps(POLICY if policy is None else policy))
    need(product in PRODUCTS and set(p)==set(POLICY),"POLICY")
    grid,look,window,age,lat,tol=(p[k] for k in ("grid_ms","momentum_ms","ofi_ms","max_age_ms","latency_ms","tolerance_ms"))
    hs=p["horizons_ms"]
    need(all(type(v) is int for v in (grid,look,window,age,lat,tol)) and 100<=grid<=60000
         and grid<=look<=900000 and 100<=window<=60000 and 0<=age<=5000 and 1<=lat<=5000 and 0<=tol<=10000
         and isinstance(hs,list) and 1<=len(hs)<=6 and all(type(h) is int and 100<=h<=900000 for h in hs)
         and hs==sorted(set(hs)),"CLOCK")
    threshold,imin=Decimal(p["momentum_bps"]),Decimal(p["imbalance_min"])
    need(threshold.is_finite() and 0<=threshold<=10000 and imin.is_finite() and 0<=imin<=1,"THRESHOLDS")
    qty,cap,fee,slip=map(units,(p["quantity"][product],p["capacity_fraction"],p["fee_bps"],p["extra_slippage_bps"]))
    need(qty>0 and 0<cap<=SCALE and 0<=fee<=100*SCALE and 0<=slip<=500*SCALE,"COSTS")
    book=IndexedBook();snap=None;last_l2=None;last_seq=None
    quotes=deque();flows=deque();rolling=0
    first=last=decision=None;prior=-1;pending=[];order=0
    decisions=eligible=missing=signals=filtered=0
    # Fixed decision opportunities, each horizon independently uses the same
    # entry. Explicit missing counts retain rejected paths.
    stats={h:{k:{"opportunities":0,"entry_rejected":0,"exit_rejected":0,"source_ended":0,
          "completed":0,"positive_net":0,"gross_bps_sum":Decimal(0),"net_bps_sum":Decimal(0)}
          for k in ("momentum","momentum_micro_filter")} for h in hs}
    def cohorts(flag):return ("momentum","momentum_micro_filter") if flag else ("momentum",)
    def tally(flag,field,h=None):
        for hh in hs if h is None else [h]:
            for key in cohorts(flag):stats[hh][key][field]+=1
    def schedule(target,kind,signal,h=None,entry=None):
        nonlocal order
        heapq.heappush(pending,(target,order,kind,signal,h,entry));order+=1
        need(len(pending)<=100000,"PENDING_BOUND")
    def state():
        b,a=book.best("bid"),book.best("offer")
        if b is None or a is None:return None
        need(b[0]<a[0],"CROSSED");return b,a
    def emit(t):
        nonlocal rolling,decisions,eligible,missing,signals,filtered
        decisions+=1;need(decisions<=200000,"DECISION_BOUND")
        while flows and flows[0][0]<t-window:rolling-=flows.popleft()[1]
        anchor=t-look
        # retain latest strictly earlier anchor quote and subsequent observations
        while len(quotes)>1 and quotes[1][0]<anchor:quotes.popleft()
        current=state()
        if snap is None or anchor<snap or t-window<snap or current is None or last_l2 is None or t-last_l2>age or not quotes:
            missing+=1;return
        at,oldmid,oldseq=quotes[0]
        if at>=anchor or anchor-at>age:
            missing+=1;return
        need(last_l2<t and oldseq<=last_seq,"FEATURE_FUTURE")
        eligible+=1
        (b,bq),(a,aq)=current
        mid=b+a
        mom=Decimal((mid-oldmid)*10000)/Decimal(oldmid)
        if mom<threshold:return
        flag=rolling>0 and Decimal(bq-aq)/Decimal(bq+aq)>=imin
        signals+=1;filtered+=int(flag)
        tally(flag,"opportunities")
        if signal_sink is not None:signal_sink((t,product,last_seq,flag))
        schedule(t+lat,"ENTRY",(t,last_seq,flag))
    with localcontext() as ctx:
        ctx.prec=80
        for seq,t,wire in packets:
            need(type(seq) is int and seq==prior+1 and type(t) is int and 0<t<2**53 and (last is None or t>=last),"ORDER")
            prior=seq;last=t
            if first is None:first=t;decision=((t+grid-1)//grid)*grid
            while decision<=t:emit(decision);decision+=grid
            if wire["channel"]!="l2_data":continue
            affected=[e for e in wire["events"] if e["product_id"]==product]
            if not affected:continue
            before=state()
            for event in affected:
                kind=event["type"]
                if kind=="snapshot" and snap is None:snap=t
                elif kind!="update" or snap is None:raise ValueError("H3_EPOCH")
                for u in event["updates"]:
                    px,q=units(u["price_level"]),units(u["new_quantity"])
                    need(px>0 and u["side"] in ("bid","offer"),"LEVEL")
                    book.change(u["side"],px,q);need(len(book.bid)+len(book.offer)<=200000,"DEPTH_BOUND")
            after=state();need(after is not None,"EMPTY_BOOK")
            last_l2,last_seq=t,seq
            (b,_),(a,_)=after
            # retain a bounded lookback; equal-clock rows keep final packet
            if quotes and quotes[-1][0]==t:quotes[-1]=(t,b+a,seq)
            else:quotes.append((t,b+a,seq))
            while len(quotes)>1 and quotes[1][0]<t-look-grid-age:quotes.popleft()
            if before is not None:
                delta=ofi(before,after);flows.append((t,delta));rolling+=delta
            while flows and flows[0][0]<t-window:rolling-=flows.popleft()[1]
            need(len(quotes)+len(flows)<=1000000,"HISTORY_BOUND")
            cache={}
            while pending and pending[0][0]<=t:
                target,_,kind,signal,h,entry=heapq.heappop(pending)
                dt,sq,flag=signal
                if t-target>tol:
                    tally(flag,"entry_rejected" if kind=="ENTRY" else "exit_rejected",h);continue
                side="BUY" if kind=="ENTRY" else "SELL"
                if side not in cache:cache[side]=walk(book.bid,book.offer,side,qty,cap,fee,slip)
                r=cache[side]
                if r["status"]!="INDICATIVE_FULL_DEPTH_QUOTE":
                    tally(flag,"entry_rejected" if kind=="ENTRY" else "exit_rejected",h);continue
                if kind=="ENTRY":
                    need(dt<t and sq<seq,"ENTRY_CAUSAL")
                    for hh in hs:schedule(t+hh+lat,"EXIT",signal,hh,(t,seq,r))
                else:
                    et,es,buy=entry;need(et<t and es<seq,"EXIT_CAUSAL")
                    bm,sm=Decimal(buy["mid"]),Decimal(r["mid"])
                    net=(Decimal(r["all_in_quote"])/Decimal(buy["all_in_quote"])-1)*10000
                    gross=(sm/bm-1)*10000
                    for key in cohorts(flag):
                        s=stats[h][key];s["completed"]+=1;s["positive_net"]+=int(net>0)
                        s["gross_bps_sum"]+=gross;s["net_bps_sum"]+=net
        for _,_,kind,signal,h,_ in pending:
            tally(signal[2],"source_ended",h)
        need(first is not None,"EMPTY")
        rows=[]
        for h in hs:
            cohorts_out={}
            for key,s in stats[h].items():
                n=s["completed"];need(s["opportunities"]==n+s["entry_rejected"]+s["exit_rejected"]+s["source_ended"],"ACCOUNTING")
                cohorts_out[key]={k:v for k,v in s.items() if not k.endswith("_sum")}
                cohorts_out[key].update(mean_arrival_mid_markout_bps=fmt(s["gross_bps_sum"]/n) if n else None,
                                        mean_indicative_net_bps=fmt(s["net_bps_sum"]/n) if n else None)
            rows.append({"horizon_ms":h,"cohorts":cohorts_out})
        return {"schema":"h3_momentum_horizon_diagnostic_v1","product":product,"policy":p,"policy_sha256":digest(p),
                "source_messages":prior+1,"source_first_ms":first,"source_last_ms":last,
                "decisions":decisions,"eligible_decisions":eligible,"missing_decisions":missing,"momentum_signals":signals,
                "micro_filtered_signals":filtered,"results":rows,"overlapping_opportunities":True,
                "portfolio_backtest":False,"actual_fills_verified":False,"selection_performed":False,
                "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
