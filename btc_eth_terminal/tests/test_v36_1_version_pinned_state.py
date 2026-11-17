"""Exact version source reconciliation, synthetic-only; no exchange admission claim."""
from collections import deque
import json
from threading import Thread
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live

SESSION='a'*32

class VersionPinnedMarketState(unittest.TestCase):
    def setUp(self):
        self.engine=LiveMarketEngine(mode='ARCHIVE_SIMULATION')
        with self.engine._cv:
            self.engine.status='HEALTHY'
            self.engine.session_id=SESSION
            self.engine.version=3
            self.engine.rows['BTC-USD']=deque([
                {'session_id':SESSION,'state_version':1,'product':'BTC-USD','best_bid':'100.00'},
                {'session_id':SESSION,'state_version':3,'product':'BTC-USD','best_bid':'100.03'}],maxlen=256)
            self.engine.rows['ETH-USD']=deque([
                {'session_id':SESSION,'state_version':2,'product':'ETH-USD','best_bid':'20.00'}],maxlen=256)
        self.server=server_for_live(self.engine)
        self.worker=Thread(target=self.server.serve_forever,daemon=True)
        self.worker.start()
        self.origin=f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(timeout=4)

    def read(self,url):
        try:
            with urlopen(self.origin+url,timeout=3) as response:
                return response.status,json.load(response)
        except HTTPError as exc:
            return exc.code,json.loads(exc.read())

    def path(self,product='BTC-USD',session=SESSION,version='1'):
        return f'/api/v36/state?product={product}&session={session}&version={version}'

    def test_exact_row_retained(self):
        code,data=self.read(self.path())
        self.assertEqual(code,200)
        self.assertEqual(data['state']['best_bid'],'100.00')
        code,newer=self.read(self.path(version='3'))
        self.assertEqual(code,200)
        self.assertEqual(newer['state']['best_bid'],'100.03')

    def test_session_mismatch_fails_closed(self):
        code,data=self.read(self.path(session='b'*32))
        self.assertEqual(code,409)
        self.assertEqual(data['error'],'RESYNC_REQUIRED')

    def test_product_isolation(self):
        code,data=self.read(self.path(product='ETH-USD',version='2'))
        self.assertEqual(code,200)
        self.assertEqual(data['state']['best_bid'],'20.00')
        code,data=self.read(self.path(product='ETH-USD',version='1'))
        self.assertEqual(code,409)

    def test_version_eviction_fails_closed(self):
        code,_=self.read(self.path(version='4'))
        self.assertEqual(code,409)
        code,_=self.read(self.path(version='999'))
        self.assertEqual(code,409)

    def test_invalid_inputs_rejected(self):
        for p in (self.path(version='0'),self.path(version='nan'),
                  self.path(product='BTCUSDT'),self.path(session='0'),
                  self.path()+'&token=foo'):
            code,_=self.read(p)
            self.assertEqual(code,400,p)

    def test_stale_source_never_exposes_retained_market_quote(self):
        with self.engine._cv:
            self.engine.status='STALE'
        code,_=self.read(self.path())
        self.assertEqual(code,409)

if __name__=='__main__':unittest.main()
