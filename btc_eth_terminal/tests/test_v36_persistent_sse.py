"""Real previously archived Coinbase messages as paced input into persistent SSE.

All observations are ARCHIVE_SIMULATION, NOT a fresh Coinbase connection.
Test chronology, independent HTTP snapshot parity and fail-closed gaps.
"""
import json
import socket
import threading
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import unittest

from v36_live_engine import LiveMarketEngine, StateRejected, MAX_RETAINED
from v36_live_gateway import server_for_live

ARCHIVE=Path(__file__).resolve().parents[1]/'backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy'


def records():
    manifest=json.loads((ARCHIVE/'manifest.json').read_bytes())
    for chunk in manifest['chunks']:
        with (ARCHIVE/chunk['file']).open('rb') as fp:
            yield from fp


def frame(reader):
    event=None;id_=None;payload=None
    for _ in range(128):
        line=reader.readline()
        if not line:raise EOFError('SSE_CLOSED_PREMATURELY')
        line=line.decode('utf-8')
        if line=='\n':
            if event:return event,id_,json.loads(payload)
            continue
        if line.startswith('event: '):event=line[7:].strip()
        elif line.startswith('id: '):id_=line[4:].strip()
        elif line.startswith('data: '):payload=line[6:].strip()
    raise AssertionError('UNBOUNDED_SSE_FRAME')


class ExistingArchivedSourceThroughPersistentSSE(unittest.TestCase):
    def setUp(self):
        if not ARCHIVE.is_dir():self.skipTest('Original immutable private archive absent')
        self.engine=LiveMarketEngine()
        self.record_list=list(records())
        index=0
        while self.engine.health()['status']!='HEALTHY':
            self.engine.consume(self.record_list[index]);index+=1
            if index>200:raise AssertionError('Archive source never admitted')
        self.index=index
        self.server=server_for_live(self.engine)
        self.runner=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.runner.start()
        self.base='http://127.0.0.1:%d' % self.server.server_address[1]

    def tearDown(self):
        if hasattr(self,'server'):
            self.server.shutdown();self.server.server_close();self.runner.join(timeout=3)

    def _get(self,route,headers=None):
        try:
            with urlopen(Request(self.base+route,headers=headers or {}),timeout=4) as r:
                return r.status,json.loads(r.read())
        except HTTPError as e:
            return e.code,json.loads(e.read())

    def test_two_clients_receive_new_real_archive_state_and_correct_replay_position(self):
        opened=[]
        try:
            for _ in range(2):
                r=urlopen(self.base+'/api/v36/stream?product=BTC-USD',timeout=5)
                opened.append(r)
                kind,_,hello=frame(r)
                self.assertEqual(kind,'source')
                self.assertEqual(hello['mode'],'ARCHIVE_SIMULATION')
                kind,id_,snapshot=frame(r)
                self.assertEqual(kind,'snapshot')
                self.assertEqual(id_,f'v36:{self.engine.session_id}:BTC-USD:{snapshot["state_version"]}')
            before=self.engine.snapshot('BTC-USD')['state']['state_version']
            # This input is genuine archived material but delivered at test pace.
            n=0
            for raw in self.record_list[self.index:]:
                self.engine.consume(raw);n+=1
                if self.engine.snapshot('BTC-USD')['state']['state_version']>before:break
            self.assertGreater(n,0)
            expected=self.engine.snapshot('BTC-USD')['state']
            for r in opened:
                for _ in range(8):
                    typ,cursor,row=frame(r)
                    if typ!='heartbeat': break
                self.assertEqual(typ,'state')
                self.assertEqual(row,expected)
                self.assertEqual(cursor,f'v36:{self.engine.session_id}:BTC-USD:{expected["state_version"]}')
            code,body=self._get('/api/v36/snapshot?product=BTC-USD')
            self.assertEqual(code,200)
            self.assertEqual(body['state'],expected)
            self.engine.transport_lost('INJECTED_TEST_OBSERVER_DISCONNECT')
            for r in opened:
                for _ in range(8):
                    kind,_,state=frame(r)
                    if kind!='heartbeat':break
                self.assertEqual(kind,'resync_required')
                self.assertEqual(state['reason'],'RESYNC_REQUIRED')
            self.assertEqual(self._get('/api/v36/snapshot?product=BTC-USD')[0],503)
        finally:
            for r in opened:r.close()

    def test_bounded_history_requires_resynchronization(self):
        snapshot=self.engine.snapshot('BTC-USD')['state']
        old=snapshot['state_version']
        with self.engine._cv:
            for i in range(MAX_RETAINED+3):
                self.engine.version+=1
                self.engine.rows['BTC-USD'].append({**snapshot,'state_version':self.engine.version})
        token=f'v36:{self.engine.session_id}:BTC-USD:{old}'
        code,data=self._get('/api/v36/stream?product=BTC-USD',{'Last-Event-ID':token})
        self.assertEqual(code,409)
        self.assertEqual(data['error'],'RESYNC_REQUIRED')
        self.assertLessEqual(len(self.engine.rows['BTC-USD']),MAX_RETAINED)

    def test_old_session_rejected_on_restart(self):
        previous=self.engine.snapshot('BTC-USD')['state']
        old=f'v36:{self.engine.session_id}:BTC-USD:{previous["state_version"]}'
        new_engine=LiveMarketEngine()
        for raw in self.record_list[:self.index]:new_engine.consume(raw)
        newserver=server_for_live(new_engine)
        t=threading.Thread(target=newserver.serve_forever,daemon=True);t.start()
        try:
            req=Request(f'http://127.0.0.1:{newserver.server_address[1]}/api/v36/stream?product=BTC-USD',headers={'Last-Event-ID':old})
            with self.assertRaises(HTTPError) as cm:urlopen(req,timeout=4)
            self.assertEqual(cm.exception.code,409)
            self.assertEqual(json.loads(cm.exception.read())['error'],'SOURCE_OR_PRODUCT_CHANGED_RESYNC_REQUIRED')
        finally:newserver.shutdown();newserver.server_close();t.join(timeout=3)

    def test_reject_live_route_when_source_unadmitted(self):
        self.engine.transport_lost('INJECTED_TEST')
        status,payload=self._get('/api/v36/snapshot?product=ETH-USD')
        self.assertEqual(status,503)
        self.assertIsNone(payload['state'])
        status,payload=self._get('/api/v36/stream?product=ETH-USD')
        self.assertEqual(status,409)
        self.assertEqual(payload['error'],'RESYNC_REQUIRED')

if __name__=='__main__':unittest.main()
