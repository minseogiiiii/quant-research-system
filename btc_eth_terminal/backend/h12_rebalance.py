"""Offline causal cash-sleeve allocation/rebalancing, indicative depth only."""
from decimal import Decimal,localcontext,ROUND_FLOOR
from collections import defaultdict
from f4_depth_buckets import SCALE,units,PRODUCTS
from f5_execution_depth import walk
from post_quote_markout import IndexedBook
from h11_covariance import RollingCovariance
from f6_liquidity_recovery import digest

POLICY={"grid_ms":10000,"window_returns":6,"max_age_ms":3000,"latency_ms":100,"tolerance_ms":1500,
        "sleeve_fraction":"0.10","turnover_fraction":"0.50","deadband_fraction":"0.05",
        "max_cost_bps":"15","fee_bps":"10","extra_slippage_bps":"2","capacity_fraction":"0.5",
        "base_increment":"0.00000001","shrinkage":"0","method":"minimum_variance"}

def need(ok,why):
    if not ok:raise ValueError("H12E_"+why)
def fmt(x):return format(x,"f")
def shrink(e,lam):
    """Fixed diagonal shrinkage, not fitted Ledoit-Wolf."""
    with localcontext() as ctx:
        ctx.prec=80
        a,c=(e["variance"][p] for p in PRODUCTS);b=e["covariance"]
        need(0<=lam<=1 and a>0 and c>0 and b*b<=a*c+Decimal("1e-70"),"COVARIANCE")
        z=(1-lam)*b
        need(a*c>z*z and a*c-z*z>a*c*Decimal("1e-8"),"DEGENERATE")
        return {"variance":dict(zip(PRODUCTS,(a,c))),"covariance":z}

def weights(e,method):
    a,c=(e["variance"][p] for p in PRODUCTS);b=e["covariance"]
    if method=="minimum_variance":x=max(Decimal(0),min(Decimal(1),(c-b)/(a+c-2*b)))
    elif method=="risk_parity":x=c.sqrt()/(a.sqrt()+c.sqrt())
    else:x=Decimal(".5")
    return dict(zip(PRODUCTS,(x,1-x)))

def validate(p):
    need(type(p) is dict and set(p)==set(POLICY),"POLICY")
    g,w,a,l,t=(p[k] for k in ("grid_ms","window_returns","max_age_ms","latency_ms","tolerance_ms"))
    need(all(type(v) is int for v in (g,w,a,l,t)) and 100<=g<=60000 and 2<=w<=120 and 0<=a<=5000
         and 1<=l<=5000 and 0<=t<=10000,"CLOCK")
    need(p["method"] in ("minimum_variance","risk_parity","equal_weight"),"METHOD")
    f={}
    for k in set(POLICY)-{"grid_ms","window_returns","max_age_ms","latency_ms","tolerance_ms","method"}:
        need(type(p[k]) is str and 0<len(p[k])<=32,"DECIMAL")
        f[k]=Decimal(p[k]);need(f[k].is_finite() and f[k]>=0,"DOMAIN")
    need(0<f["sleeve_fraction"]<=Decimal(".15") and 0<f["turnover_fraction"]<=1
         and 0<=f["deadband_fraction"]<=1 and 0<=f["shrinkage"]<=1
         and 0<=f["max_cost_bps"]<=600 and 0<=f["fee_bps"]<=100 and 0<=f["extra_slippage_bps"]<=500
         and 0<f["capacity_fraction"]<=1 and 0<f["base_increment"]<=1,"LIMITS")
    return f

def project(packets,policy=None,*,initial_cash="10000"):
    p=POLICY if policy is None else policy
    f=validate(p);initial=Decimal(initial_cash)
    need(initial.is_finite() and 0<initial<=Decimal("1000000000"),"CASH")
    cash=initial;inventory=dict.fromkeys(PRODUCTS,0);events=[];plans=[]
    counts=defaultdict(int);pending={};total_cost=Decimal(0);turnover=Decimal(0)
    grid,age,lat,tol=(p[k] for k in ("grid_ms","max_age_ms","latency_ms","tolerance_ms"))
    cov=RollingCovariance(grid,p["window_returns"])
    books={k:IndexedBook() for k in PRODUCTS};clocks=dict.fromkeys(PRODUCTS,None)
    cap,fee,slip,step=map(units,(p["capacity_fraction"],p["fee_bps"],p["extra_slippage_bps"],p["base_increment"]))
    sleeve=initial*f["sleeve_fraction"];prior=-1;last=first=decision=None
    def mid(k):
        b=books[k];return Decimal(b.best("bid")[0]+b.best("offer")[0])/(2*SCALE)
    def quote(k,side,q):
        b=books[k];r=walk(b.bid,b.offer,side,q,cap,fee,slip)
        if r["status"]!="INDICATIVE_FULL_DEPTH_QUOTE":return None
        v=Decimal(r["all_in_quote"]);m=mid(k)*Decimal(q)/SCALE
        cost=v-m if side=="BUY" else m-v
        need(cost>=0 and m>0,"COST")
        return v,m,cost
    def expire(t):
        for k,(_,_,target,_,_) in list(pending.items()):
            if t>target+tol:del pending[k];counts["expired"]+=1
    def plan(t,e,mids):
        nonlocal cash
        counts["decisions"]+=1
        if pending:counts["pending_skips"]+=1;return
        if e is None:counts["unavailable"]+=1;return
        try:adj=shrink(e,f["shrinkage"])
        except ValueError:counts["degenerate"]+=1;return
        w=weights(adj,p["method"])
        delta={k:sleeve*w[k]-mid(k)*Decimal(inventory[k])/SCALE for k in PRODUCTS}
        for k in PRODUCTS:
            if abs(delta[k])<sleeve*f["deadband_fraction"]:delta[k]=Decimal(0)
        gross=sum(map(abs,delta.values()))
        if not gross:counts["deadband_skips"]+=1;return
        factor=min(Decimal(1),sleeve*f["turnover_fraction"]/gross)
        intent=[]
        for k in PRODUCTS:
            if not delta[k]:continue
            side="BUY" if delta[k]>0 else "SELL"
            q=int((abs(delta[k])*factor/mid(k)*SCALE/step).to_integral_value(rounding=ROUND_FLOOR))*step
            if side=="SELL":q=min(q,inventory[k])
            if q==0:continue
            r=quote(k,side,q)
            if r is None:counts["decision_depth_rejected"]+=1;continue
            v,m,cost=r
            if cost*10000>m*f["max_cost_bps"]:counts["decision_cost_rejected"]+=1;continue
            intent.append((k,side,q,clocks[k],fmt(m),fmt(cost)))
        if not intent:counts["no_intents"]+=1;return
        counts["batches"]+=1
        need(len(plans)<200000,"PLAN_BOUND")
        plans.append((t,[(k,s,q,seq,m,c) for k,s,q,seq,m,c in intent]))
        for k,side,q,stamp,m,c in intent:
            pending[k]=(side,q,t+lat,t,stamp);counts["intents"]+=1
    with localcontext() as ctx:
        ctx.prec=80
        for seq,t,wire in packets:
            need(type(seq) is int and seq==prior+1 and type(t) is int and t>0 and (last is None or t>=last),"ORDER")
            prior=seq;last=t
            if first is None:first=t;decision=((t+grid-1)//grid)*grid
            while decision<=t:
                expire(decision)
                mids={k:books[k].best("bid")[0]+books[k].best("offer")[0] for k in PRODUCTS
                      if clocks[k] is not None and clocks[k][0]<decision and decision-clocks[k][0]<=age}
                e=cov.observe(decision,mids)
                plan(decision,e,mids);decision+=grid
            expire(t)
            if wire["channel"]!="l2_data":continue
            affected=set()
            for event in wire["events"]:
                k=event["product_id"];need(k in books,"PRODUCT");b=books[k]
                need((clocks[k] is None and event["type"]=="snapshot") or (clocks[k] is not None and event["type"]=="update"),"EPOCH")
                for u in event["updates"]:
                    px,q=units(u["price_level"]),units(u["new_quantity"])
                    need(px>0 and u["side"] in ("bid","offer"),"LEVEL")
                    b.change(u["side"],px,q);need(len(b.bid)+len(b.offer)<=200000,"DEPTH_BOUND")
                clocks[k]=(t,seq);affected.add(k)
            for k in affected:
                b=books[k];need(b.bid and b.offer and b.best("bid")[0]<b.best("offer")[0],"BOOK")
            for side in ("SELL","BUY"):
                for k in sorted(affected):
                    item=pending.get(k)
                    if item is None or item[0]!=side or t<item[2]:continue
                    _,q,target,dt,stamp=item;del pending[k]
                    need(dt<t and stamp[0]<dt and stamp[1]<seq,"CAUSAL")
                    r=quote(k,side,q)
                    if r is None:counts["arrival_depth_rejected"]+=1;continue
                    v,m,cost=r
                    if cost*10000>m*f["max_cost_bps"]:counts["arrival_cost_rejected"]+=1;continue
                    if side=="BUY" and v>cash:counts["cash_rejected"]+=1;continue
                    if side=="BUY":cash-=v;inventory[k]+=q
                    else:need(inventory[k]>=q,"INVENTORY");inventory[k]-=q;cash+=v
                    total_cost+=cost;turnover+=m;counts["fills"]+=1
                    events.append((side,k,q,fmt(v),fmt(m),fmt(cost),seq,t))
                    need(cash>=0 and len(events)<=400000,"LEDGER_BOUND")
        need(first is not None,"EMPTY")
        counts["source_ended"]+=len(pending)
        need(counts["intents"]==sum(counts[k] for k in ("fills","expired","arrival_depth_rejected","arrival_cost_rejected","cash_rejected","source_ended")),"INTENT_ACCOUNTING")
        # Reconstruct the cash, inventory, and cost ledger from executed quotes.
        wc=initial;wi=dict.fromkeys(PRODUCTS,0);costs=Decimal(0);tr=Decimal(0)
        for side,k,q,v,m,c,_,_ in events:
            v,m,c=map(Decimal,(v,m,c));need(c==(v-m if side=="BUY" else m-v),"COST_WITNESS")
            if side=="BUY":wc-=v;wi[k]+=q
            else:wc+=v;wi[k]-=q
            need(wc>=0 and wi[k]>=0,"WITNESS")
            costs+=c;tr+=m
        need((wc,wi,costs,tr)==(cash,inventory,total_cost,turnover),"LEDGER")
        fresh=all(not inventory[k] or clocks[k] is not None and last-clocks[k][0]<=age for k in PRODUCTS)
        exposure=sum((mid(k)*Decimal(inventory[k])/SCALE for k in PRODUCTS if inventory[k]),Decimal(0)) if fresh else None
        eq=None if exposure is None else cash+exposure
        return {"schema":"h12e_cost_aware_rebalance_v1","policy":p,"policy_sha256":digest(p),"source_messages":prior+1,
                "source_first_ms":first,"source_last_ms":last,"counts":dict(counts),"initial_cash":fmt(initial),
                "ending_cash":fmt(cash),"inventory_base_units":inventory,"terminal_mark_equity":None if eq is None else fmt(eq),
                "terminal_mark_pnl":None if eq is None else fmt(eq-initial),"terminal_mark_exposure":None if exposure is None else fmt(exposure),
                "trading_cost_quote":fmt(total_cost),"turnover_quote":fmt(turnover),"plan_sha256":digest(plans),"event_sha256":digest(events),
                "covariance":cov.receipt(),"cash_inventory_witness":True,"terminal_liquidation_performed":False,
                "actual_fills_verified":False,"alpha_confirmed":False,"oos_approved":False,"execution_authorized":False}
