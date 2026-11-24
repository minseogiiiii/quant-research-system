import copy
import unittest
from decimal import Decimal
from h9_portfolio import simulate,collect
from h3_momentum_horizons import POLICY
def fixture():
    p=copy.deepcopy(POLICY);p.update(grid_ms=100,latency_ms=10,tolerance_ms=100,max_age_ms=1000,horizons_ms=[100,900],
        quantity={"BTC-USD":"1","ETH-USD":"1"},capacity_fraction="1",fee_bps="0",extra_slippage_bps="0")
    rows=[]
    for t in (1000,1050,1150,1250,1350,1450):
        events=[]
        for product in ("BTC-USD","ETH-USD"):
            events.append(dict(product_id=product,type="snapshot" if t==1000 else "update",
                updates=[dict(side="bid",price_level="99",new_quantity="10"),dict(side="offer",price_level="101",new_quantity="10")]))
        rows.append((len(rows),t,dict(channel="l2_data",events=events)))
    sig=[(1100,"BTC-USD",1,True),(1100,"ETH-USD",1,False),(1200,"BTC-USD",2,True)]
    return rows,sig,p
class Portfolio(unittest.TestCase):
    def test_shared_cash_overlap_and_unresolved(self):
        rows,sig,p=fixture()
        r=simulate(rows,sig,p,initial_cash="150")
        a,b,c,d=r["portfolios"]
        self.assertEqual(a["counts"]["cash_rejected"],1)
        self.assertEqual(a["counts"]["overlap_rejected"],1)
        self.assertEqual(a["counts"]["exits"],1)
        self.assertEqual(Decimal(a["realized_net_quote"]),-2)
        self.assertEqual(Decimal(a["ending_cash"]),148)
        self.assertEqual(b["counts"]["signals"],2)
        self.assertIsNone(c["final_flat_return"])
        self.assertEqual(c["unresolved_products"],["BTC-USD"])
        self.assertTrue(all(x["cash_inventory_witness"] for x in r["portfolios"]))
        self.assertFalse(r["execution_authorized"])
    def test_future_signal_rejected(self):
        rows,sig,p=fixture();sig[0]=(1100,"BTC-USD",2,True)
        with self.assertRaisesRegex(ValueError,"SIGNAL_CAUSAL"):simulate(rows,sig,p)
    def test_h3_signal_sink_does_not_change_results(self):
        from test_h3_momentum_horizons import Horizon
        from h3_momentum_horizons import project
        rows,p=Horizon().setup();sink=[]
        self.assertEqual(project(rows,"BTC-USD",p),project(rows,"BTC-USD",p,signal_sink=sink.append))
        self.assertGreater(len(sink),0)
