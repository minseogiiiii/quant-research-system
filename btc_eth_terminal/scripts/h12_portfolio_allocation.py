"""Offline two-asset allocation diagnostics with an exact Fincept ERC witness.

No expected-return forecasts, annualization, order sizing, or authorization.
Fincept's single reviewed function is isolated from unrelated service imports.
"""
import ast
import hashlib
import json
import math
from pathlib import Path
from decimal import Decimal,localcontext
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
VENDOR=ROOT/"vendor/fincept"
PIN="914134de5fd227fe66969c4591ac9ca7e461cdf4"
BLOB="273df40785f71a92c7f0e4b0370fc5e6b8e2382e"
PRODUCTS=("BTC-USD","ETH-USD")
def need(ok,why):
    if not ok:raise ValueError("H12_"+why)

def erc_function():
    raw=(VENDOR/"portfolio/additional_optimizers.py").read_bytes()
    need(hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()==BLOB,"VENDOR_PIN")
    manifest=json.loads((VENDOR/"UPSTREAM.json").read_text())
    need(manifest["commit"]==PIN and manifest["license"]=="AGPL-3.0"
         and any(e["git_blob"]==BLOB and e["local"]=="btc_eth_terminal/vendor/fincept/portfolio/additional_optimizers.py" for e in manifest["files"]),"VENDOR_MANIFEST")
    tree=ast.parse(raw)
    defs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="equal_risk_contribution_weights"]
    need(len(defs)==1 and not defs[0].decorator_list,"VENDOR_FUNCTION")
    # Execute only the reviewed, immutable function AST, never the surrounding
    # vendor service, imports or dynamically supplied source.
    ns={"np":np,"__builtins__":{"range":range,"float":float,"min":min,"ValueError":ValueError}}
    exec(compile(ast.Module(body=defs,type_ignores=[]),"<pinned-fincept-erc>","exec"),ns)
    return ns["equal_risk_contribution_weights"]

def matrix(e):
    with localcontext() as ctx:
        ctx.prec=80
        a=Decimal(e["variance"][PRODUCTS[0]]);c=Decimal(e["variance"][PRODUCTS[1]]);b=Decimal(e["covariance"])
        need(all(v.is_finite() for v in (a,b,c)) and 0<a<=1 and 0<c<=1 and b*b<a*c,"COVARIANCE")
        scale=max(a,c);S=np.array([[float(a/scale),float(b/scale)],[float(b/scale),float(c/scale)]])
        eig=np.linalg.eigvalsh(S)
        need(eig[0]>1e-8*eig[1],"ILL_CONDITIONED")
        return a,b,c,S

def metrics(w,a,b,c):
    x,y=w
    ca=x*(a*x+b*y);cb=y*(b*x+c*y);v=ca+cb
    need(v>0 and abs(x+y-1)<Decimal("1e-12") and min(x,y)>=0,"WEIGHTS")
    return {"weights":dict(zip(PRODUCTS,map(float,w))),"variance_per_grid":float(v),
            "volatility_per_grid":float(v.sqrt()),"variance_contribution":dict(zip(PRODUCTS,map(float,(ca,cb)))),
            "risk_contribution_fraction":dict(zip(PRODUCTS,map(float,(ca/v,cb/v))))}

def allocate(e):
    with localcontext() as ctx:
        ctx.prec=80
        a,b,c,S=matrix(e)
        x=max(Decimal(0),min(Decimal(1),(c-b)/(a+c-2*b)))
        # KKT derivative and numerical exhaustive grid witness.
        slope=(a+c-2*b)*x+b-c
        need((x==0 and slope>=0) or (x==1 and slope<=0) or (0<x<1 and abs(slope)<max(a,c)*Decimal("1e-65")),"MINVAR_KKT")
        grid=np.linspace(0,1,10001);v=S[0,0]*grid**2+2*S[0,1]*grid*(1-grid)+S[1,1]*(1-grid)**2
        z=np.array([float(x),float(1-x)])
        need(float(z@S@z)<=float(v.min())+1e-12,"MINVAR_GRID")
        # For two strictly positive risk assets, ERC has the closed form
        # w1*sigma1 == w2*sigma2, independent of their correlation.
        r=c.sqrt()/(a.sqrt()+c.sqrt());ref=np.array([float(r),float(1-r)])
        actual=erc_function()(S)
        need(actual.shape==(2,) and np.isfinite(actual).all() and np.all(actual>0)
             and abs(float(actual.sum())-1)<1e-10 and np.allclose(actual,ref,rtol=1e-6,atol=1e-8),"ERC_PARITY")
        rp=metrics((r,1-r),a,b,c)
        need(max(abs(v-.5) for v in rp["risk_contribution_fraction"].values())<1e-10,"ERC_CONTRIBUTION")
        mv=metrics((x,1-x),a,b,c);eq=metrics((Decimal(".5"),Decimal(".5")),a,b,c)
        return {"schema":"h12_two_asset_allocation_v1","minimum_variance":mv,"risk_parity":rp,"equal_weight":eq,
                "minimum_variance_kkt_and_grid":True,"fincept_erc_witness":True,
                "fincept_weights":dict(zip(PRODUCTS,map(float,actual))),"vendor_commit":PIN,"vendor_blob":BLOB,
                "annualized":False,"fully_invested_risky_sleeve_only":True,"costs_included":False,
                "orders_generated":False,"forecast_validated":False,"oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
