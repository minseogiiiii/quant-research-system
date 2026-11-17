"""Node v29/v36 collector: bounded disk and abrupt SIGKILL fail-closed.

All source frames synthetic. No private capture uploaded or classified LIVE.
"""
import json,os,shutil,signal,subprocess,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class StorageFailClosed(unittest.TestCase):
 def start(self,work,**overrides):
  node=shutil.which('node')
  if not node:self.skipTest('Node not available')
  env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),
       'SOAK_DURATION_SECONDS':'15','SOAK_RAW_BUDGET_BYTES':'1000000000',
       'SOAK_ARCHIVE_BUDGET_BYTES':'2000000000','SOAK_DISK_RESERVE_BYTES':'0',
       'SOAK_CHUNK_LIMIT_BYTES':'8000000','SOAK_TEST_FIXTURE':'1',
       'SOAK_TEST_DISCONNECT_AFTER_MESSAGES':'0'}
  env.update(overrides)
  return subprocess.Popen([node,'--import',str(ROOT/'tests/v36_mock_coinbase_websocket.mjs'),
                           str(ROOT/'scripts/btc_eth_multiday_v20.mjs')],
              env=env,cwd=work,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 def archive(self,work):
  matches=list((Path(work)/'soak_evidence').glob('coinbase_btc_eth_soak_*'))
  self.assertEqual(len(matches),1)
  return matches[0]
 def test_archive_budget_exhaustion(self):
  with tempfile.TemporaryDirectory(prefix='v36_budget_') as folder:
   p=self.start(folder,SOAK_RAW_BUDGET_BYTES='1600',SOAK_ARCHIVE_BUDGET_BYTES='2000')
   try:p.communicate(timeout=8)
   finally:
    if p.poll() is None:p.kill();p.communicate()
   self.assertNotEqual(p.returncode,0)
   m=json.loads((self.archive(folder)/'manifest.json').read_bytes())
   self.assertFalse(m['ready_for_research'])
   self.assertEqual(m['finish_reason'],'NONRECOVERABLE_STORAGE_LIMIT')
   self.assertGreaterEqual(len(m['faults']),1)
 def test_reserve_exhaustion(self):
  with tempfile.TemporaryDirectory(prefix='v36_reserve_') as folder:
   p=self.start(folder,SOAK_DISK_RESERVE_BYTES='1000000000000')
   try:p.communicate(timeout=8)
   finally:
    if p.poll() is None:p.kill();p.communicate()
   self.assertNotEqual(p.returncode,0)
   m=json.loads((self.archive(folder)/'manifest.json').read_bytes())
   self.assertFalse(m['ready_for_research'])
   self.assertEqual(m['finish_reason'],'NONRECOVERABLE_STORAGE_LIMIT')
   self.assertTrue(any('DISK_RESERVE_EXCEEDED' in f['reason'] for f in m['faults']))
 def test_sigkill_retains_partial_and_restart_uses_new_archive(self):
  with tempfile.TemporaryDirectory(prefix='v36_sigkill_') as folder:
   a=Path(folder)/'a';a.mkdir();p=self.start(a)
   try:
    until=time.monotonic()+5
    while time.monotonic()<until:
     partial=list((a/'soak_evidence').glob('coinbase_btc_eth_soak_*/chunks/*.partial'))
     if partial and partial[0].stat().st_size>500:break
     time.sleep(.02)
    else:self.fail('No active partial file created')
    os.kill(p.pid,signal.SIGKILL);p.communicate(timeout=3)
   finally:
    if p.poll() is None:p.kill();p.communicate()
   old=self.archive(a);m=json.loads((old/'manifest.json').read_bytes())
   self.assertEqual(m['status'],'CAPTURING_UNVERIFIED')
   self.assertFalse(m['ready_for_research'])
   self.assertTrue(list((old/'chunks').glob('*.partial')))
   b=Path(folder)/'b';b.mkdir();q=self.start(b)
   try:
    time.sleep(.55);q.send_signal(signal.SIGTERM);q.communicate(timeout=7)
   finally:
    if q.poll() is None:q.kill();q.communicate()
   self.assertNotEqual(old.resolve(),self.archive(b).resolve())
   self.assertTrue(list((old/'chunks').glob('*.partial')))
if __name__=='__main__':unittest.main()
