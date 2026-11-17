"""GitHub runnable synthetic-only v36 source admission and fault tests.

No historical archive or external exchange is consulted. These are NOT real
Coinbase live-source tests and must not be reported as such.
"""
from __future__ import annotations
import json,hashlib,time,unittest
from datetime import datetime,timezone,timedelta
from v36_live_engine import LiveMarketEngine,StateRejected

PRODUCTS=['BTC-USD','ETH-USD']

def line(seq:int,channel:str,events:list,*,base:datetime|None=None,conn:int=0):
    at=(base or datetime(2026,11,14,tzinfo=timezone.utc))+timedelta(milliseconds=seq*50)
    wire=json.dumps({'sequence_num':seq,'channel':channel,'events':events},separators=(',',':'))
    return (json.dumps({'connection_id':conn,'received_utc':at.isoformat(timespec='milliseconds').replace('+00:00','Z'),
      'local_monotonic_ns':str(10**12+seq*100_000_000),
      'sha256':hashlib.sha256(wire.encode()).hexdigest(),'raw_text':wire},separators=(',',':'))+'\n').encode()

def book(base):
    return ([{'side':'bid','price_level':str(base-n),'new_quantity':str(n+1)} for n in range(10)]+
            [{'side':'offer','price_level':str(base+1+n),'new_quantity':str(n+1)} for n in range(10)])

def boot():
    packets=[line(0,'l2_data',[{'type':'snapshot','product_id':'BTC-USD','updates':book(100)}]),
       line(1,'l2_data',[{'type':'snapshot','product_id':'ETH-USD','updates':book(200)}]),
       line(2,'subscriptions',[{'subscriptions':{'level2':PRODUCTS,'market_trades':PRODUCTS,'heartbeats':['heartbeats']}}]),
       line(3,'heartbeats',[{'heartbeat_counter':1}])]
    e=LiveMarketEngine()
    for p in packets:e.consume(p)
    return e,packets

class OfflineAdmissionContract(unittest.TestCase):
    def test_dual_snapshots_ack_heartbeat_enable_only_archive_simulation(self):
        e,_=boot();h=e.health();self.assertEqual(h['status'],'HEALTHY')
        self.assertEqual(h['mode'],'ARCHIVE_SIMULATION');self.assertFalse(h['live_feed'])
        for product in PRODUCTS:
            self.assertIsNotNone(e.snapshot(product)['state']);self.assertFalse(e.snapshot(product)['state']['trading_enabled'])

    def test_crossed_book_or_sequence_gap_quarantines_source(self):
        e,_=boot()
        with self.assertRaises(StateRejected):
            e.consume(line(8,'heartbeats',[]))
        self.assertEqual(e.health()['status'],'QUARANTINED')
        self.assertIsNone(e.snapshot('BTC-USD')['state'])
        e,_=boot()
        with self.assertRaises(StateRejected):
            e.consume(line(4,'l2_data',[{'type':'update','product_id':'BTC-USD',
                'updates':[{'side':'bid','price_level':'500','new_quantity':'10'}]}]))
        self.assertIsNone(e.snapshot('BTC-USD')['state'])

    def test_observer_disconnect_suppresses_prices(self):
        e,_=boot();e.transport_lost('SYNTHETIC_SOCKET_DISCONNECT')
        self.assertEqual(e.health()['status'],'RECONNECTING')
        self.assertIsNone(e.snapshot('ETH-USD')['state'])

    def test_archive_data_cannot_be_upgraded_by_a_flag(self):
        with self.assertRaises(StateRejected):LiveMarketEngine(mode='LIVE_SOURCE')
        with self.assertRaises(StateRejected):LiveMarketEngine(mode='LIVE_SOURCE',collector_runtime_verified=True,
                                                             collector_script_sha256='fake')

if __name__=='__main__':unittest.main()
