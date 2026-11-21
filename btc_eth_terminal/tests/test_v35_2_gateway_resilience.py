"""Actual Coinbase archive, bounded gateway clients, SSE cursor and recovery.

No synthetic market prices or network exchange subscriptions are used.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import socket
from threading import Thread
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen, Request

from v35_replay_service import replay_capture
from v35_unified_gateway import server_for, CLIENT_SOCKET_TIMEOUT_SECONDS

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'backend'/'data'/'live_v14_clean_120s'/'coinbase_btc_eth_soak_wsgXRy'
EXTERNAL_PIN='8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5'


class ReadOnlyGatewayReliability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_dir():raise unittest.SkipTest('Real pinned Coinbase archive unavailable')
        if sha256((SOURCE/'manifest.json').read_bytes()).hexdigest()!=EXTERNAL_PIN:
            raise RuntimeError('Frozen archive changed: do not run as verified')
        cls.dataset=replay_capture(SOURCE,EXTERNAL_PIN)
        cls.server=server_for(cls.dataset)
        cls.thread=Thread(target=cls.server.serve_forever,kwargs={'poll_interval':0.05},daemon=True)
        cls.thread.start()
        cls.address=('127.0.0.1',cls.server.server_address[1])
        cls.base='http://127.0.0.1:'+str(cls.address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)
        if cls.thread.is_alive():raise RuntimeError('Gateway worker did not stop')

    def get(self,url,headers=None):
        try:
            with urlopen(Request(self.base+url,headers=headers or {}),timeout=6) as r:
                return r.status,r.read()
        except HTTPError as e:return e.code,e.read()

    def metric(self):
        status,data=self.get('/api/v35/metrics')
        self.assertEqual(status,200)
        return json.loads(data)

    def test_real_source_metadata_and_permissions(self):
        status,data=self.get('/api/v35/health');self.assertEqual(status,200)
        h=json.loads(data)
        self.assertEqual(h['source_manifest_sha256'],EXTERNAL_PIN)
        self.assertEqual(h['messages'],5177)
        self.assertEqual(h['mode'],'ARCHIVE_REPLAY')
        self.assertFalse(h['live_feed']);self.assertFalse(h['trading_enabled'])
        m=self.metric()
        self.assertEqual(m['max_clients'],8)
        self.assertEqual(m['sse_queue_depth'],0)
        self.assertEqual(m['socket_timeout_seconds'],CLIENT_SOCKET_TIMEOUT_SECONDS)
        self.assertFalse(m['trading_enabled'])

    def test_sse_success_and_observability(self):
        before=self.metric()
        code,body=self.get('/api/v35/stream?product=BTC-USD&limit=4')
        self.assertEqual(code,200)
        text=body.decode()
        self.assertEqual(text.count('event: state\n'),4)
        self.assertIn('event: replay_end\n',text)
        after=self.metric()
        self.assertEqual(after['sse_started'],before['sse_started']+1)
        self.assertEqual(after['sse_completed'],before['sse_completed']+1)
        self.assertEqual(after['sse_aborted'],before['sse_aborted'])

    def test_version_resync_and_unknown_sources(self):
        start=self.metric()['resync_required']
        for path in ['/api/v35/stream?product=BTC-USD&after=888888&limit=2',
                     '/api/v35/history?product=ETH-USD&after=888888&limit=2']:
            code,data=self.get(path)
            self.assertEqual(code,409)
            self.assertEqual(json.loads(data)['error'],'RESYNC_REQUIRED')
        self.assertEqual(self.metric()['resync_required'],start+2)
        self.assertEqual(self.get('/api/v35/orders')[0],404)
        self.assertEqual(self.get('/api/v35/metrics?evil=x')[0],400)

    def test_resume_sequence_and_eof_finite(self):
        first=self.dataset.rows['BTC-USD'][1]['state_version']
        code,data=self.get('/api/v35/stream?product=BTC-USD&limit=2',{'Last-Event-ID':EXTERNAL_PIN+':BTC-USD:'+str(first)})
        self.assertEqual(code,200)
        self.assertIn('id: '+EXTERNAL_PIN+':BTC-USD:'+str(self.dataset.rows['BTC-USD'][2]['state_version']),data.decode())
        last=self.dataset.rows['BTC-USD'][-1]['state_version']
        code,data=self.get('/api/v35/stream?product=BTC-USD&limit=2',{'Last-Event-ID':EXTERNAL_PIN+':BTC-USD:'+str(last)})
        self.assertEqual(code,200)
        self.assertEqual(data.decode().count('event: state\n'),0)
        self.assertIn('"has_more":false',data.decode())

    def test_concurrent_readers_bounded_no_data_corruption(self):
        expected=self.dataset.final_state_sha256
        def fetch(i):
            route='/api/v35/snapshot?product='+('BTC-USD' if i%2 else 'ETH-USD')
            code,body=self.get(route)
            return code, json.loads(body)['state']['source_manifest_sha256']
        with ThreadPoolExecutor(max_workers=6) as executor:
            all_results=list(executor.map(fetch,range(30)))
        self.assertTrue(all(code==200 and pin==EXTERNAL_PIN for code,pin in all_results))
        self.assertEqual(self.dataset.final_state_sha256,expected)
        self.assertLessEqual(self.metric()['peak_connections'],8)

    def test_idle_header_peers_release_slots_after_timeout(self):
        # Deliberately incomplete headers, no data or order writes.
        before=self.metric()['active_connections']
        peers=[]
        try:
            for _ in range(4):
                sock=socket.create_connection(self.address,timeout=2)
                sock.sendall(b'GET /api/v35/health HTTP/1.1\r\nHost: localhost\r\nX-Partial: ')
                peers.append(sock)
            start=time.monotonic()
            observed=False
            while time.monotonic()-start<1.5:
                if self.metric()['active_connections']>=before+3:
                    observed=True;break
                time.sleep(0.05)
            self.assertTrue(observed,'Idle sockets failed to occupy bounded workers')
            # Socket timeout must release worker slots even if clients stay idle.
            recovered=False
            for _ in range(90):
                m=self.metric()
                if m['active_connections']<=before+1:
                    recovered=True;break
                time.sleep(0.075)
            self.assertTrue(recovered,'Client timeout failed to release gateway resources')
        finally:
            for sock in peers:sock.close()

    def test_gateway_restart_retains_same_immutable_market_state(self):
        original=self.dataset.snapshot('ETH-USD')['state']
        other=server_for(self.dataset)
        thread=Thread(target=other.serve_forever,daemon=True)
        try:
            thread.start()
            with urlopen(f'http://127.0.0.1:{other.server_address[1]}/api/v35/snapshot?product=ETH-USD',timeout=4) as r:
                row=json.load(r)['state']
            self.assertEqual(row,original)
            self.assertEqual(row['source_manifest_sha256'],EXTERNAL_PIN)
        finally:
            other.shutdown();other.server_close();thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()
