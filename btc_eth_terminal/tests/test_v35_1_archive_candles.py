"""Independent real-source receiver-clock OHLCV and HTTP contract validation.

No candle may be labeled official exchange OHLCV or a trading signal.
Historical market_trades snapshot data is deliberately excluded from flow.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Thread
import unittest
from urllib.request import urlopen

from v35_replay_service import replay_capture, StateRejected
from v35_unified_gateway import server_for

DATA=Path(os.environ.get('V34_1_BACKEND_DATA', str(Path(__file__).resolve().parents[1] / 'backend' / 'data')))
CLEAN=next((DATA/'live_v14_clean_120s').glob('coinbase*'),None) if (DATA/'live_v14_clean_120s').is_dir() else None

def independent_candles(root):
    # Uses original JSONL, never the Book/replay/candle implementation under test.
    from datetime import datetime
    output={p:{} for p in ('BTC-USD','ETH-USD')}
    manifest=json.loads((root/'manifest.json').read_bytes())
    first=None;last=None
    for chunk in manifest['chunks']:
        for line in (root/chunk['file']).read_bytes().splitlines():
            envelope=json.loads(line)
            recv=int(datetime.fromisoformat(envelope['received_utc'].replace('Z','+00:00')).timestamp()*1000)
            if first is None:first=recv
            last=recv
            msg=json.loads(envelope['raw_text'])
            if msg['channel']!='market_trades':continue
            for event in msg['events']:
                if event['type']!='update':continue
                for t in event['trades']:
                    product=t['product_id'];start=recv//60000*60000
                    price=Decimal(t['price']);size=Decimal(t['size'])
                    values=output[product].setdefault(start,[])
                    values.append((price,size))
    lower=(first+59999)//60000*60000
    answer={}
    for product,buckets in output.items():
        rows=[]
        for start,events in sorted(buckets.items()):
            if start<lower or start+60000>last:continue
            prices=[p for p,_ in events]
            rows.append((start,prices[0],max(prices),min(prices),prices[-1],sum((q for _,q in events),Decimal(0)),len(events)))
        answer[product]=rows
    return answer

class AuthenticCandleContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if CLEAN is None:raise unittest.SkipTest('No pinned authentic archive available')
        cls.manifest_sha=sha256((CLEAN/'manifest.json').read_bytes()).hexdigest()
        cls.ds=replay_capture(CLEAN,cls.manifest_sha)

    def test_receiver_trade_candles_match_independent_update_only_replay(self):
        expected=independent_candles(CLEAN)
        for product in ('BTC-USD','ETH-USD'):
            actual=self.ds.candles[product]
            self.assertEqual(len(actual),len(expected[product]))
            self.assertGreaterEqual(len(actual),1)
            for row,baseline in zip(actual,expected[product]):
                self.assertEqual(row['time'],baseline[0])
                self.assertEqual(row['available_at'],row['time']+60000)
                self.assertEqual([Decimal(row[k]) for k in ('open','high','low','close','volume')],list(baseline[1:6]))
                self.assertEqual(row['observed_trade_count'],baseline[6])
                self.assertEqual(row['source_manifest_sha256'],self.manifest_sha)
                self.assertFalse(row['trading_enabled'])
                self.assertFalse(row['live_feed'])
                self.assertEqual(row['mode'],'ARCHIVE_REPLAY')

    def test_startup_snapshot_not_falsely_counted_as_new_flow(self):
        expected=independent_candles(CLEAN)
        for product in ('BTC-USD','ETH-USD'):
            self.assertEqual(sum(x['observed_trade_count'] for x in self.ds.candles[product]),sum(x[6] for x in expected[product]))

    def test_candle_page_read_only_bounds_and_identity(self):
        self.assertEqual(self.ds.candles_page('BTC-USD')['schema'],'v35_1_archived_trade_candles_v1')
        for bad in ('SOL-USD','BTCUSDT',''):
            with self.assertRaises(StateRejected):self.ds.candles_page(bad)
        for bad in (0,-1,401,True,5.3):
            with self.assertRaises(StateRejected):self.ds.candles_page('ETH-USD',bad)

    def test_candles_http_exact_source_and_safe_query(self):
        server=server_for(self.ds)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        host=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            for product in ('BTC-USD','ETH-USD'):
                with urlopen(host+f'/api/v35/candles?product={product}&limit=400',timeout=5) as response:
                    self.assertEqual(response.status,200)
                    data=json.load(response)
                self.assertEqual(data['product'],product)
                self.assertEqual(data['source_manifest_sha256'],self.manifest_sha)
                self.assertEqual(data['rows'],list(self.ds.candles[product]))
            from urllib.error import HTTPError
            for suffix in ('product=SOL-USD','product=BTC-USD&limit=10000','product=BTC-USD&limit=0','product=BTC-USD&token=x'):
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(host+'/api/v35/candles?'+suffix,timeout=5)
                self.assertEqual(ctx.exception.code,400)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

if __name__=='__main__':unittest.main()