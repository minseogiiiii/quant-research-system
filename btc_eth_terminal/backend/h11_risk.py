"""Opt-in indicative entry-risk limits; exits are never denied by risk.

Caps are fractions of initial capital, not guarantees of future exposure or
loss. Mark-to-mid observed risk only, stale marks reject new exposure.
"""
from decimal import Decimal
from f4_depth_buckets import SCALE,PRODUCTS

POLICY={"per_product_fraction":"0.10","gross_fraction":"0.15",
        "max_loss_fraction":"0.02","max_drawdown_fraction":"0.02"}
def need(ok,why):
    if not ok:raise ValueError("H11_"+why)
def validate_policy(p):
    need(type(p) is dict and set(p)==set(POLICY),"POLICY")
    vals={}
    for k,v in p.items():
        need(type(v) is str and 0<len(v)<=32,"VALUE")
        x=Decimal(v);need(x.is_finite() and 0<x<=1,"DOMAIN");vals[k]=x
    need(vals["per_product_fraction"]<=vals["gross_fraction"],"CAP_ORDER")
    return vals

class Risk:
    def __init__(self,initial,policy):
        self.initial=initial;self.limits=validate_policy(policy)
        self.peak=initial;self.max_dd=Decimal(0);self.max_loss=Decimal(0)
        self.peak_exposure=Decimal(0);self.latch=None;self.latch_time=None
        self.stale_observations=0;self.market_cap_breach_observations=0
        self.rejections={}
    def marks(self,t,pos,books,clocks,age,strict=False):
        values={}
        for product,(_,q) in pos.items():
            stamp,_=clocks[product];book=books[product]
            if stamp is None or stamp>t or (strict and stamp==t) or t-stamp>age or not book.bid or not book.offer:
                return None
            values[product]=Decimal(book.best("bid")[0]+book.best("offer")[0])*q/(2*SCALE)
        return values
    def observe(self,t,cash,pos,books,clocks,age,strict=False):
        values=self.marks(t,pos,books,clocks,age,strict)
        if values is None:
            self.stale_observations+=1;return None
        gross=sum(values.values(),Decimal(0));equity=cash+gross
        need(equity>0,"EQUITY")
        self.peak=max(self.peak,equity)
        dd=1-equity/self.peak;loss=1-equity/self.initial
        self.max_dd=max(self.max_dd,dd);self.max_loss=max(self.max_loss,loss)
        self.peak_exposure=max(self.peak_exposure,gross)
        if gross>self.initial*self.limits["gross_fraction"] or any(v>self.initial*self.limits["per_product_fraction"] for v in values.values()):
            self.market_cap_breach_observations+=1
        if self.latch is None:
            if loss>=self.limits["max_loss_fraction"]:self.latch="risk_loss_latch"
            elif dd>=self.limits["max_drawdown_fraction"]:self.latch="risk_drawdown_latch"
            if self.latch is not None:self.latch_time=t
        return values
    def admit(self,t,cash,pos,books,clocks,age,product,amount,q):
        need(product in PRODUCTS and product not in pos and amount>0 and q>0,"ENTRY")
        values=self.observe(t,cash,pos,books,clocks,age)
        # Require both product books fresh at entry: no stale cross-asset
        # risk assessment. Use the current packet, never a future quote.
        fresh=all(clocks[k][0] is not None and 0<=t-clocks[k][0]<=age
                  and books[k].bid and books[k].offer for k in PRODUCTS)
        reason=self.latch
        if reason is None and (values is None or not fresh):reason="risk_stale"
        if reason is None:
            book=books[product];mid=Decimal(book.best("bid")[0]+book.best("offer")[0])*q/(2*SCALE)
            new=max(amount,mid)
            if new>self.initial*self.limits["per_product_fraction"]:reason="risk_product_cap"
            elif sum(values.values(),Decimal(0))+new>self.initial*self.limits["gross_fraction"]:reason="risk_gross_cap"
        if reason:self.rejections[reason]=self.rejections.get(reason,0)+1
        return reason
    def receipt(self):
        return {"schema":"h11_observed_risk_v1","entry_rejections":self.rejections,
                "entry_latch":self.latch,"latch_receiver_ms":self.latch_time,
                "stale_mark_observations":self.stale_observations,
                "observed_cap_breach_observations":self.market_cap_breach_observations,
                "observed_peak_exposure":format(self.peak_exposure,"f"),
                "observed_max_drawdown":format(self.max_dd,"f"),
                "observed_max_initial_loss":format(self.max_loss,"f"),
                "forced_liquidation":False,"risk_limits_guaranteed":False,
                "position_sizing":"FIXED_QUANTITY_REJECT_NOT_RESIZE"}
