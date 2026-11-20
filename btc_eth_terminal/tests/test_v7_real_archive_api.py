"""Read-only integration checks against the actual 2026-11-08 HTTP-source archive.

These are source-to-local-API tests, NOT live UI, historical availability,
execution, order-book or trading strategy tests.
"""
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import bridge

MARKET_HASHES = {
    'BTC_COINBASE_SPOT': '9869f1833669492fa4156fb51ffd529cfd42a6c870a54215c238a3f9a12bb353',
    'ETH_COINBASE_SPOT': '17782415aaebf7291a81cfcac3e4c313a99fb9c471fd8f3fd7bbc7f781f34f98',
}


class RealCoinbaseLocalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    @classmethod
    def get_json(cls, path, params=None):
        url = cls.base + path + ('?' + urlencode(params) if params else '')
        with urlopen(url, timeout=8) as response:
            return response.status, json.load(response)

    def test_catalog_exposes_only_two_read_only_real_spot_series(self):
        code, data = self.get_json('/api/catalog')
        self.assertEqual(code, 200)
        self.assertFalse(data['trading_enabled'])
        self.assertTrue(data['historical_only'])
        self.assertEqual({row['symbol'] for row in data['datasets']}, set(MARKET_HASHES))

    def test_actual_source_to_http_bars_same_hash(self):
        for symbol, digest in MARKET_HASHES.items():
            with self.subTest(symbol=symbol):
                code, payload = self.get_json('/api/bars', {
                    'symbol': symbol, 'resolution': '5',
                    'to': 1794182400000, 'from': 1794096000000, 'countBack': 300,
                })
                self.assertEqual(code, 200)
                self.assertEqual(len(payload['bars']), 288)
                self.assertEqual(payload['csv_sha256'], digest)
                self.assertEqual(payload['bars'][0]['time'], 1794096000000)
                self.assertEqual(payload['bars'][-1]['time'], 1794182100000)
                self.assertTrue(all('available_at' not in b for b in payload['bars']))

    def test_historical_asof_rejected_and_no_post_order_endpoint(self):
        query = urlencode({'symbol':'BTC_COINBASE_SPOT','resolution':'5',
                           'to':1794182400000,'from':0,'countBack':5,
                           'asof':1794182400000})
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + '/api/bars?' + query, timeout=8)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('available_at', caught.exception.read().decode())
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + '/api/order', timeout=8)
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
