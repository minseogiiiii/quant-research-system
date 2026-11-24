import unittest
from decimal import Decimal
from h12_joint_risk import dollar_vol,JointRisk
from post_quote_markout import IndexedBook
from f4_depth_buckets import SCALE
from h9_portfolio import simulate
from h11_risk import POLICY as RISK
from test_h11_covariance import POLICY as SIZING
from test_h9_portfolio import fixture
def estimate(b):
    return {"variance":{"BTC-USD":Decimal(".01"),"ETH-USD":Decimal(".04")},"covariance":Decimal(b)}
class Joint(unittest.TestCase):
    def test_covariance_dollar_risk(self):
        x={"BTC-USD":Decimal(100),"ETH-USD":Decimal(50)}
        self.assertAlmostEqual(float(dollar_vol(estimate("0"),x)),float(Decimal(200).sqrt()),places=12)
        self.assertGreater(dollar_vol(estimate(".01"),x),dollar_vol(estimate("0"),x))
    def test_combined_exposure_and_missing_reset(self):
        books={p:IndexedBook() for p in ("BTC-USD","ETH-USD")}
        for b in books.values():b.change("bid",99*SCALE,SCALE);b.change("offer",101*SCALE,SCALE)
        clocks=dict.fromkeys(books,(1050,1))
        r=JointRisk(Decimal(1000),".025");r.update(1000,estimate(".01"),100)
        self.assertIsNone(r.admit(1050,"ETH-USD",Decimal(1),{},books,clocks,100))
        self.assertEqual(r.admit(1050,"ETH-USD",Decimal(1),{"BTC-USD":(Decimal(100),Decimal(1))},books,clocks,100),"joint_volatility_cap")
        r.update(1100,None,100)
        self.assertEqual(r.admit(1150,"BTC-USD",Decimal(1),{},books,clocks,100),"joint_covariance_unavailable")
        self.assertEqual(r.receipt()["checks"],3)
    def test_integrated_degenerate_reject_and_existing_mode(self):
        rows,sig,p=fixture();rows.append((6,1550,rows[-1][2]))
        sig=[(1300,"BTC-USD",3,True)]
        risk=dict(RISK,per_product_fraction=".8",gross_fraction=".9")
        sizing=dict(SIZING,window_returns=2,risk_budget_fraction=".001",max_notional_fraction=".05")
        a=simulate(rows,sig,p,initial_cash="1000",risk_policy=risk,sizing_policy=sizing)
        b=simulate(rows,sig,p,initial_cash="1000",risk_policy=risk,sizing_policy=sizing,joint_budget_fraction=".01")
        self.assertEqual(a["portfolios"][0]["counts"]["entries"],1)
        self.assertEqual(b["portfolios"][0]["counts"]["entries"],0)
        self.assertEqual(b["portfolios"][0]["joint_risk"]["rejections"]["joint_covariance_degenerate"],1)
        with self.assertRaises(ValueError):simulate(rows,sig,p,joint_budget_fraction=".01")
