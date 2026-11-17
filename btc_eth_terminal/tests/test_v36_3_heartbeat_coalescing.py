"""200 irrelevant source heartbeats must not emit 200 SSE heartbeats."""
import sys
import time
import unittest
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from v36_live_gateway import server_for_live
sys.path.insert(0,str(ROOT/'tests'))
from test_v36_ci_synthetic_admission import boot,line


class CoalescedSSEHeartbeat(unittest.TestCase):
    def test_unrelated_source_heartbeats_cannot_flood_an_idle_product(self):
        e,_=boot()
        server=server_for_live(e)
        worker=Thread(target=server.serve_forever,daemon=True)
        worker.start()
        url=f'http://127.0.0.1:{server.server_address[1]}/api/v36/stream?product=BTC-USD'
        try:
            with urlopen(url,timeout=5) as stream:
                def next_event():
                    kind=None
                    while True:
                        raw=stream.readline(200001)
                        if not raw:return None
                        s=raw.decode().strip()
                        if s.startswith('event: '):kind=s[7:]
                        if not s:return kind
                self.assertEqual(next_event(),'source')
                self.assertEqual(next_event(),'snapshot')
                for i in range(4,204):
                    e.consume(line(i,'heartbeats',[{'heartbeat_counter':i}]))
                    time.sleep(.003)
                e.transport_lost('DELIBERATE_TEST_END')
                beats=0
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    kind=next_event()
                    if kind=='heartbeat':beats+=1
                    if kind=='resync_required' or kind is None:break
                self.assertLessEqual(beats,2,f'SSE_HEARTBEAT_FLOOD:{beats}')
        finally:
            e.transport_lost('TEST_FINAL')
            server.shutdown()
            server.server_close()
            worker.join(timeout=4)

if __name__=='__main__':unittest.main()
