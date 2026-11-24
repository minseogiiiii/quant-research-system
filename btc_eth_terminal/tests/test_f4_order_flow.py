import unittest
from decimal import Decimal
from unittest.mock import patch

from f4_order_flow import aggregate, build

def trade(tid,side,qty,px="100.1",product="BTC-USD"):
    return dict(trade_id=tid,side=side,size=qty,price=px,product_id=product,time="2026-11-24T00:00:00Z")

def packet(seq,t,kind,*trades):
    return seq,t,dict(channel="market_trades",events=[dict(type=kind,trades=list(trades))])

def project(packets,**kwargs):
    args=dict(product="BTC-USD",start_ms=1000,end_ms=5000,bucket_ms=1000,price_width="1")
    args.update(kwargs)
    return aggregate(packets,**args)

class FlowContracts(unittest.TestCase):
    def test_direction_exact_cvd_and_replayed_trade(self):
        a=trade("a","SELL","0.100000000000000001")
        b=trade("b","BUY","0.4")
        c=trade("c","SELL","0.2","101")
        packets=[packet(0,1000,"snapshot",trade("s","BUY","9")),
                 packet(1,1100,"update",a,b,trade("eth","SELL","5",product="ETH-USD")),
                 packet(2,1200,"update",a),packet(3,3100,"update",c)]
        r=project(packets)
        first,last=r["rows"]
        self.assertEqual(first["buy_base"],a["size"])
        self.assertEqual(first["sell_base"],b["size"])
        self.assertEqual(Decimal(first["delta_base"]),Decimal(a["size"])-Decimal(b["size"]))
        self.assertEqual(Decimal(last["observed_cvd_base"]),sum((Decimal(x["size"])*(1 if x["side"]=="SELL" else -1) for x in (a,b,c)),Decimal(0)))
        self.assertEqual(r["buckets_without_observed_trades_ms"],[2000,4000])
        self.assertEqual(r["snapshot_trades_excluded"],1)
        self.assertEqual(r["identical_update_replays_excluded"],1)
        self.assertEqual(r["unique_window_update_trades"],3)
        self.assertFalse(r["live_feed"])

    def test_conflicting_ids_and_same_packet_duplicate(self):
        a=trade("a","SELL","1")
        for ps in ([packet(0,1100,"update",a,a)],
                   [packet(0,1100,"update",a),packet(1,1200,"update",trade("a","BUY","1"))],
                   [packet(0,1100,"snapshot",a),packet(1,1200,"update",a)]):
            with self.assertRaisesRegex(ValueError,"CONFLICT"):
                project(ps)

    def test_boundaries_and_pre_window_dedupe(self):
        a=trade("a","SELL","1")
        r=project([packet(0,999,"update",a),packet(1,1000,"update",a,trade("b","BUY","2")),
                   packet(2,5000,"update",trade("c","SELL","4"))])
        self.assertEqual(r["unique_window_update_trades"],1)
        self.assertEqual(r["rows"][0]["observed_cvd_base"],"-2")

    def test_invalid_sequence_clock_quantity_and_side(self):
        for ps in ([packet(1,1100,"update")],[packet(0,1100,"update"),packet(1,1000,"update")],
                   [packet(0,1100,"update",trade("a","SELL","0"))],
                   [packet(0,1100,"update",trade("a","X","1"))]):
            with self.assertRaises(ValueError):
                project(ps)

    def test_epoch_gate(self):
        with patch("f4_order_flow.verify_capture",return_value={"source_eligible_for_state_replay":True,"connections":2}):
            with self.assertRaisesRegex(ValueError,"SINGLE_ADMITTED_EPOCH"):
                build(".","a"*64)
