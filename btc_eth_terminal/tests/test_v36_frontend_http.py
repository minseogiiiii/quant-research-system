"""SHA-pinned v36 UI plus original v34 and v35.1 route preservation.

These tests do NOT assert browser DOM correctness. Real Chromium access is a
separate blocked environmental gate when localhost navigation is forbidden.
"""
import hashlib,json,threading,unittest
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live
from v36_frontend_integrity import v36_asset,LIVE_JS,LIVE_CSS,DIST,MANIFEST,PINNED_MANIFEST_SHA256
from v35_1_frontend_integrity import candidate_asset
from v34_frontend_integrity import verify_build

class FrontendReadOnlyParity(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.e=LiveMarketEngine();cls.server=server_for_live(cls.e)
  cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.worker.start()
  cls.base='http://127.0.0.1:%d'%cls.server.server_address[1]
 @classmethod
 def tearDownClass(cls):
  cls.server.shutdown();cls.server.server_close();cls.worker.join(timeout=2)
 def get(self,path):
  try:
   with urlopen(self.base+path,timeout=4) as r:return r.status,r.read()
  except HTTPError as err:return err.code,err.read()
 def test_v36_bundle_exact_SHA_and_HTTP(self):
  self.assertEqual(hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),PINNED_MANIFEST_SHA256)
  for path in ('/research/v36',LIVE_JS,LIVE_CSS):
   expected,ct=v36_asset(path)
   code,actual=self.get(path)
   self.assertEqual(code,200,path)
   self.assertEqual(actual,expected,path)
   self.assertGreater(len(actual),200)
 def test_old_historical_assets_unchanged(self):
  self.assertIsNotNone(verify_build())
  for path in ('/research','/terminal','/research/v35.1'):
   code,body=self.get(path)
   self.assertEqual(code,200,path)
   self.assertIn(b'<html',body.lower())
  self.assertGreater(len(candidate_asset('/research/v35.1')[0]),100)
 def test_health_has_no_fake_prices_before_admission(self):
  self.assertEqual(self.get('/api/v36/snapshot?product=BTC-USD')[0],503)
  code,body=self.get('/api/v36/health')
  self.assertEqual(code,200)
  d=json.loads(body)
  self.assertEqual(d['status'],'CONNECTING')
  self.assertFalse(d['live_feed'])
  self.assertFalse(d['trading_enabled'])
 def test_no_orders_and_disallowed_market(self):
  self.assertEqual(self.get('/api/v36/order')[0],404)
  self.assertEqual(self.get('/api/v36/snapshot?product=BTCUSDT')[0],400)
 def test_integrity_rejects_mutated_asset_in_separate_staging_copy(self):
  # Never mutate or rename the original SHA-pinned dist assets.
  from tempfile import TemporaryDirectory
  from shutil import copytree
  import v36_frontend_integrity as mod
  with TemporaryDirectory() as temp:
   staging=Path(temp)/'dist';copytree(DIST,staging)
   css=staging/LIVE_CSS.lstrip('/')
   css.write_bytes(css.read_bytes()+b'/*intentional mutation*/')
   original=mod.DIST
   try:
    mod.DIST=staging
    code,body=self.get('/research/v36')
    self.assertEqual(code,503)
    self.assertIn(b'V36_FRONTEND_INTEGRITY_FAILURE',body)
   finally:mod.DIST=original
  self.assertEqual(self.get('/research/v36')[0],200)
if __name__=='__main__':unittest.main()
