"""Separate read-only v36 source-qualified operational metrics from v35."""
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
root=str(Path(__file__).resolve().parents[1]/'backend')
sys.path.insert(0,root)
from v36_live_engine import LiveMarketEngine
sys.path.insert(0,root)
from v36_live_gateway import server_for_live

class LiveMetricsContract(unittest.TestCase):
    def test_archive_simulation_metrics_must_not_claim_real_live(self):
        self.assert_metrics(LiveMarketEngine(),'ARCHIVE_SIMULATION',False)

    def test_unadmitted_live_source_metrics_must_show_connecting_not_healthy(self):
        engine=LiveMarketEngine(mode='LIVE_SOURCE',collector_runtime_verified=True,
                                collector_script_sha256='f'*64)
        self.assert_metrics(engine,'LIVE_SOURCE',True)

    def assert_metrics(self,engine,mode,live_feed):
        server=server_for_live(engine)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            try:
                with urlopen(f'http://127.0.0.1:{server.server_address[1]}/api/v36/metrics',timeout=3) as resp:
                    self.assertEqual(resp.status,200);m=json.load(resp)
            except HTTPError as exc:
                self.fail('Missing operational source metrics: HTTP '+str(exc.code))
            self.assertEqual(m['schema'],'v36_live_transport_metrics_v1')
            self.assertEqual(m['mode'],mode)
            self.assertEqual(m['live_feed'],live_feed)
            self.assertEqual(m['source_status'],'CONNECTING')
            self.assertFalse(m['source_healthy'])
            self.assertFalse(m['trading_enabled'])
            self.assertEqual(m['max_clients'],10)
            self.assertEqual(m['max_sse_clients'],8)
            self.assertEqual(m['max_events_per_burst'],64)
            self.assertEqual(m['max_stream_seconds'],300)
            self.assertEqual(m['state_retention_per_product'],256)
            self.assertGreaterEqual(m['accepted_requests'],1)
        finally:
            server.shutdown();server.server_close();worker.join(timeout=3)

if __name__=='__main__':unittest.main()
