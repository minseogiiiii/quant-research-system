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
    def __init__(self,horizon,filtered,initial):
        self.h=horizon;self.filtered=filtered;self.initial=initial;self.cash=initial
        self.pending={};self.pos={};self.blocked=False;self.events=[]
        self.count=defaultdict(int);self.realized=Decimal(0);self.peak=initial
        self.dd=Decimal(0);self.grid=0;self.missing=0;self.last_equity=None
        self.peak_exposure=Decimal(0)
    def signal(self,t,product,seq,flag,lat):
        if self.filtered and not flag:return
        self.count["signals"]+=1
        if self.blocked:self.count["blocked_signal"]+=1
        elif product in self.pending or product in self.pos:self.count["overlap_rejected"]+=1
        else:self.pending[product]=("BUY",t+lat,seq);self.count["entry_intents"]+=1
    def reject(self,product,side,reason):
        del self.pending[product];self.count[reason]+=1
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
    def fill(self,t,seq,affected,books,qty,cap,fee,slip,lat,cache):
        # Exits before entries, then alphabetical product priority on one packet.
        for side in ("SELL","BUY"):
            for product in sorted(affected):
                intent=self.pending.get(product)
                if intent is None or intent[0]!=side or t<intent[1]:continue
                need(seq>intent[2],"FUTURE_QUOTE")
                if side=="BUY" and self.blocked:
                    self.reject(product,side,"blocked_entry");continue
                k=(product,side)
                if k not in cache:
                    b=books[product];cache[k]=walk(b.bid,b.offer,side,qty[product],cap,fee,slip)
                r=cache[k]
                if r["status"]!="INDICATIVE_FULL_DEPTH_QUOTE":
                    self.reject(product,side,"depth_"+side.lower());continue
                amount=Decimal(r["all_in_quote"]);q=Decimal(r["requested_base"])
                need(amount>0 and q==Decimal(qty[product])/SCALE,"QUOTE")
                if side=="BUY":
                    if amount>self.cash:
                        self.reject(product,side,"cash_rejected");continue
                    need(product not in self.pos,"DUP_POSITION")
                    self.cash-=amount;self.pos[product]=(amount,q)
                    self.pending[product]=("SELL",t+self.h+lat,seq);self.count["entries"]+=1
                else:
                    cost,base=self.pos.pop(product);need(base==q,"INVENTORY")
                    self.cash+=amount;self.realized+=amount-cost;del self.pending[product];self.count["exits"]+=1
                self.events.append((side,product,fmt(amount),fmt(q)))
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
        return {"horizon_ms":self.h,"cohort":"momentum_micro_filter" if self.filtered else "momentum",
                "counts":dict(c),"initial_cash":fmt(self.initial),"ending_cash":fmt(cash),
                "realized_net_quote":fmt(pnl),"unresolved_products":sorted(self.pos),
                "final_flat_return":fmt(cash/self.initial-1) if flat else None,
                "grid_points":self.grid,"missing_equity_points":self.missing,
                "last_observed_grid_equity":None if self.last_equity is None else fmt(self.last_equity),
                "grid_max_drawdown":fmt(self.dd) if self.grid and not self.missing else None,
                "observed_grid_peak_exposure":fmt(self.peak_exposure),
                "cash_inventory_witness":True,"event_digest":digest(self.events)}

def simulate(packets,signals,policy=None,*,initial_cash="10000"):
    p=POLICY if policy is None else policy
    initial=Decimal(initial_cash)
    need(initial.is_finite() and initial>0 and initial<=Decimal("1000000000"),"INITIAL")
    qty={k:units(p["quantity"][k]) for k in PRODUCTS}
    cap,fee,slip=map(units,(p["capacity_fraction"],p["fee_bps"],p["extra_slippage_bps"]))
    grid,lat,tol,age=(p[k] for k in ("grid_ms","latency_ms","tolerance_ms","max_age_ms"))
    need(len(signals)<=400000 and signals==sorted(signals) and len({(x[0],x[1]) for x in signals})==len(signals),"SIGNALS")
    books={k:IndexedBook() for k in PRODUCTS};clocks=dict.fromkeys(PRODUCTS,(None,None))
    portfolios=[Portfolio(h,f,initial) for h in p["horizons_ms"] for f in (False,True)]
    pos=0;prior=-1;last=first=decision=None
    with localcontext() as ctx:
        ctx.prec=80
        for seq,t,wire in packets:
            need(type(seq) is int and seq==prior+1 and type(t) is int and (last is None or t>=last),"ORDER")
            prior=seq;last=t
            if first is None:first=t;decision=((t+grid-1)//grid)*grid
            while decision<=t:
                for a in portfolios:a.expire(decision,tol);a.mark(decision,books,clocks,age)
                while pos<len(signals) and signals[pos][0]<=decision:
                    dt,product,sq,flag=signals[pos];stamp,actual=clocks[product]
                    need(dt==decision and stamp is not None and stamp<dt and dt-stamp<=age and actual==sq and sq<seq and type(flag) is bool,"SIGNAL_CAUSAL")
                    for a in portfolios:a.signal(dt,product,sq,flag,lat)
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
            for a in portfolios:a.fill(t,seq,affected,books,qty,cap,fee,slip,lat,cache)
        need(first is not None and pos==len(signals),"SOURCE_INVENTORY")
        return {"schema":"h9_portfolio_diagnostic_v1","policy_sha256":digest(p),"source_messages":prior+1,
                "source_first_ms":first,"source_last_ms":last,"signals":len(signals),"signal_sha256":digest(signals),
                "portfolios":[a.finish() for a in portfolios],"capital_shared_across_products":True,
                "capital_shared_across_scenarios":False,"actual_fills_verified":False,"selection_performed":False,
                "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
