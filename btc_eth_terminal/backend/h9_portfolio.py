"""Causal, shared-cash indicative portfolio simulation on one native epoch.
Each horizon/cohort is a separate counterfactual portfolio, never aggregated.
One position per product, no borrowing, no real orders or simulated maker fills.
"""
from decimal import Decimal,localcontext
from collections import defaultdict
from f4_depth_buckets import units,SCALE,PRODUCTS
from f5_execution_depth import walk
from post_quote_markout import IndexedBook
from h3_momentum_horizons import POLICY,project as signal_project
from h11_risk import Risk,validate_policy
from h11_covariance import RollingCovariance,validate_sizing,size_units
from f6_liquidity_recovery import digest

def need(ok,why):
    if not ok:raise ValueError("H9_"+why)
def fmt(v):return format(v,"f")
def collect(factory,policy=None):
    p=POLICY if policy is None else policy
    rows=[];reports={}
    for product in PRODUCTS:
        signals=[]
        r=signal_project(factory(),product,p,signal_sink=signals.append)
        need(len(signals)==r["momentum_signals"] and sum(x[3] for x in signals)==r["micro_filtered_signals"],"SIGNAL_COUNT")
        rows.extend(signals);reports[product]=r
    return sorted(rows),reports

class Portfolio:
    def __init__(self,horizon,filtered,initial,risk_policy=None,sizing_policy=None):
        self.h=horizon;self.filtered=filtered;self.initial=initial;self.cash=initial
        self.pending={};self.pos={};self.blocked=False;self.events=[]
        self.count=defaultdict(int);self.realized=Decimal(0);self.peak=initial
        self.dd=Decimal(0);self.grid=0;self.missing=0;self.last_equity=None
        self.peak_exposure=Decimal(0)
        self.risk=Risk(initial,risk_policy) if risk_policy is not None else None
        self.sizing_policy=sizing_policy;self.entry_qty={};self.sizing_events=[]
    def signal(self,t,product,seq,flag,lat,sizing=None):
        if self.filtered and not flag:return
        self.count["signals"]+=1
        if self.blocked:self.count["blocked_signal"]+=1
        elif product in self.pending or product in self.pos:self.count["overlap_rejected"]+=1
        else:
            self.pending[product]=("BUY",t+lat,seq);self.count["entry_intents"]+=1
            if self.sizing_policy is not None:
                q=sizing
                if q is None:self.reject(product,"BUY","sizing_unavailable")
                elif q==0:self.reject(product,"BUY","sizing_below_minimum")
                else:
                    self.entry_qty[product]=q
                    self.sizing_events.append((t,product,seq,q))
    def reject(self,product,side,reason):
        del self.pending[product];self.entry_qty.pop(product,None);self.count[reason]+=1
        if side=="SELL":self.blocked=True;self.count["failed_exits"]+=1
        else:self.count["failed_entries"]+=1
    def expire(self,t,tol):
        for product,(side,target,_) in list(self.pending.items()):
            if t>target+tol:self.reject(product,side,"timeout_"+side.lower())
    def mark(self,t,books,clocks,age):
        self.grid+=1;exposure=Decimal(0)
        for product,(_,qty) in self.pos.items():
            stamp,_=clocks[product]
            if stamp is None or not stamp<t or t-stamp>age:
                self.missing+=1;self.last_equity=None;return
            b=books[product]
            exposure+=Decimal(b.best("bid")[0]+b.best("offer")[0])*qty/(2*SCALE)
        eq=self.cash+exposure;need(eq>0,"EQUITY")
        self.peak=max(self.peak,eq);self.dd=max(self.dd,1-eq/self.peak)
        self.peak_exposure=max(self.peak_exposure,exposure)
        self.last_equity=eq
    def fill(self,t,seq,affected,books,qty,cap,fee,slip,lat,cache,clocks=None,age=None):
        # Exits before entries, then alphabetical product priority on one packet.
        for side in ("SELL","BUY"):
            for product in sorted(affected):
                intent=self.pending.get(product)
                if intent is None or intent[0]!=side or t<intent[1]:continue
                need(seq>intent[2],"FUTURE_QUOTE")
                if side=="BUY" and self.blocked:
                    self.reject(product,side,"blocked_entry");continue
                requested=qty[product]
                if self.sizing_policy is not None:
                    requested=self.entry_qty[product] if side=="BUY" else int(self.pos[product][1]*SCALE)
                k=(product,side,requested)
                if k not in cache:
                    b=books[product];cache[k]=walk(b.bid,b.offer,side,requested,cap,fee,slip)
                r=cache[k]
                if r["status"]!="INDICATIVE_FULL_DEPTH_QUOTE":
                    self.reject(product,side,"depth_"+side.lower());continue
                amount=Decimal(r["all_in_quote"]);q=Decimal(r["requested_base"])
                need(amount>0 and q==Decimal(requested)/SCALE,"QUOTE")
                if side=="BUY":
                    if self.risk is not None:
                        reason=self.risk.admit(t,self.cash,self.pos,books,clocks,age,product,amount,q)
                        if reason:
                            self.reject(product,side,reason);continue
                    if amount>self.cash:
                        self.reject(product,side,"cash_rejected");continue
                    need(product not in self.pos,"DUP_POSITION")
                    self.entry_qty.pop(product,None)
                    self.cash-=amount;self.pos[product]=(amount,q)
                    self.pending[product]=("SELL",t+self.h+lat,seq);self.count["entries"]+=1
                else:
                    cost,base=self.pos.pop(product);need(base==q,"INVENTORY")
                    self.cash+=amount;self.realized+=amount-cost;del self.pending[product];self.count["exits"]+=1
                self.events.append((side,product,fmt(amount),fmt(q)))
                if self.risk is not None:self.risk.observe(t,self.cash,self.pos,books,clocks,age)
                need(self.cash>=0 and len(self.pos)<=2,"CAPITAL")
    def finish(self):
        for product,(side,_,_) in list(self.pending.items()):
            self.reject(product,side,"source_ended_"+side.lower())
        # Separate scalar accounting witness from event log.
        cash=self.initial;positions={};pnl=Decimal(0)
        for side,p,a,q in self.events:
            a,q=Decimal(a),Decimal(q)
            if side=="BUY":
                need(p not in positions and cash>=a,"WITNESS_BUY")
                cash-=a;positions[p]=(a,q)
            else:
                cost,base=positions.pop(p);need(base==q,"WITNESS_SELL")
                cash+=a;pnl+=a-cost
        need(cash==self.cash and positions==self.pos and pnl==self.realized,"WITNESS")
        c=self.count
        need(c["signals"]==c["blocked_signal"]+c["overlap_rejected"]+c["entry_intents"]
             and c["entry_intents"]==c["entries"]+c["failed_entries"]
             and c["entries"]==c["exits"]+len(self.pos)
             and c["failed_exits"]==len(self.pos),"COUNTS")
        flat=not self.pos
        if flat:need(cash==self.initial+pnl,"FLAT_CASH")
        result={"horizon_ms":self.h,"cohort":"momentum_micro_filter" if self.filtered else "momentum",
                "counts":dict(c),"initial_cash":fmt(self.initial),"ending_cash":fmt(cash),
                "realized_net_quote":fmt(pnl),"unresolved_products":sorted(self.pos),
                "final_flat_return":fmt(cash/self.initial-1) if flat else None,
                "grid_points":self.grid,"missing_equity_points":self.missing,
                "last_observed_grid_equity":None if self.last_equity is None else fmt(self.last_equity),
                "grid_max_drawdown":fmt(self.dd) if self.grid and not self.missing else None,
                "observed_grid_peak_exposure":fmt(self.peak_exposure),
                "cash_inventory_witness":True,"event_digest":digest(self.events)}
        if self.risk is not None:result["risk"]=self.risk.receipt()
        if self.sizing_policy is not None:
            result["sizing"]={"frozen_intents":len(self.sizing_events),"intent_sha256":digest(self.sizing_events),
                "min_base_units":min((x[3] for x in self.sizing_events),default=None),
                "max_base_units":max((x[3] for x in self.sizing_events),default=None),
                "unavailable":c["sizing_unavailable"],"below_minimum":c["sizing_below_minimum"]}
        return result

def simulate(packets,signals,policy=None,*,initial_cash="10000",risk_policy=None,sizing_policy=None):
    p=POLICY if policy is None else policy
    initial=Decimal(initial_cash)
    need(initial.is_finite() and initial>0 and initial<=Decimal("1000000000"),"INITIAL")
    qty={k:units(p["quantity"][k]) for k in PRODUCTS}
    cap,fee,slip=map(units,(p["capacity_fraction"],p["fee_bps"],p["extra_slippage_bps"]))
    grid,lat,tol,age=(p[k] for k in ("grid_ms","latency_ms","tolerance_ms","max_age_ms"))
    need(len(signals)<=400000 and signals==sorted(signals) and len({(x[0],x[1]) for x in signals})==len(signals),"SIGNALS")
    books={k:IndexedBook() for k in PRODUCTS};clocks=dict.fromkeys(PRODUCTS,(None,None))
    if risk_policy is not None:validate_policy(risk_policy)
    if sizing_policy is not None:
        need(risk_policy is not None,"SIZING_REQUIRES_RISK")
        validate_sizing(sizing_policy)
    covariance=RollingCovariance(grid,sizing_policy["window_returns"]) if sizing_policy is not None else None
    portfolios=[Portfolio(h,f,initial,risk_policy,sizing_policy) for h in p["horizons_ms"] for f in (False,True)]
    pos=0;prior=-1;last=first=decision=None
    with localcontext() as ctx:
        ctx.prec=80
        for seq,t,wire in packets:
            need(type(seq) is int and seq==prior+1 and type(t) is int and (last is None or t>=last),"ORDER")
            prior=seq;last=t
            if first is None:first=t;decision=((t+grid-1)//grid)*grid
            while decision<=t:
                estimate=None
                if covariance is not None:
                    mids={}
                    for product in PRODUCTS:
                        stamp,_=clocks[product];book=books[product]
                        if stamp is not None and stamp<decision and decision-stamp<=age and book.bid and book.offer:
                            mids[product]=book.best("bid")[0]+book.best("offer")[0]
                    estimate=covariance.observe(decision,mids)
                for a in portfolios:
                    a.expire(decision,tol);a.mark(decision,books,clocks,age)
                    if a.risk is not None:a.risk.observe(decision,a.cash,a.pos,books,clocks,age,strict=True)
                while pos<len(signals) and signals[pos][0]<=decision:
                    dt,product,sq,flag=signals[pos];stamp,actual=clocks[product]
                    need(dt==decision and stamp is not None and stamp<dt and dt-stamp<=age and actual==sq and sq<seq and type(flag) is bool,"SIGNAL_CAUSAL")
                    sizing=None if estimate is None else size_units(estimate,product,mids[product],initial,qty[product],sizing_policy)
                    for a in portfolios:a.signal(dt,product,sq,flag,lat,sizing)
                    pos+=1
                decision+=grid
            for a in portfolios:a.expire(t,tol)
            if wire["channel"]!="l2_data":continue
            affected=set()
            for e in wire["events"]:
                product=e["product_id"];need(product in books,"PRODUCT")
                book=books[product];stamp,_=clocks[product]
                need((stamp is None and e["type"]=="snapshot") or (stamp is not None and e["type"]=="update"),"EPOCH")
                for u in e["updates"]:
                    px,q=units(u["price_level"]),units(u["new_quantity"])
                    need(px>0 and u["side"] in ("bid","offer"),"LEVEL")
                    book.change(u["side"],px,q)
                    need(len(book.bid)+len(book.offer)<=200000,"DEPTH_BOUND")
                clocks[product]=(t,seq);affected.add(product)
            for product in affected:
                b=books[product];need(b.bid and b.offer and b.best("bid")[0]<b.best("offer")[0],"BOOK")
            cache={}
            for a in portfolios:
                if a.risk is not None:a.risk.observe(t,a.cash,a.pos,books,clocks,age)
                a.fill(t,seq,affected,books,qty,cap,fee,slip,lat,cache,clocks,age)
        need(first is not None and pos==len(signals),"SOURCE_INVENTORY")
        result={"schema":"h9_portfolio_diagnostic_v1","policy_sha256":digest(p),"source_messages":prior+1,
                "source_first_ms":first,"source_last_ms":last,"signals":len(signals),"signal_sha256":digest(signals),
                "portfolios":[a.finish() for a in portfolios],"capital_shared_across_products":True,
                "capital_shared_across_scenarios":False,"actual_fills_verified":False,"selection_performed":False,
                "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
        if risk_policy is not None:result["risk_policy"]=risk_policy;result["risk_policy_sha256"]=digest(risk_policy)
        if covariance is not None:
            result["sizing_policy"]=sizing_policy;result["sizing_policy_sha256"]=digest(sizing_policy)
            result["covariance"]=covariance.receipt()
        return result
