"""Operator safety: only a real public read-only capture with host proof can run."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('v25_operator_test_module', ROOT/'scripts'/'v25_capture_operator.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OperatorTests(TestCase):
    def test_unavailable_clock_does_not_self_certify(self):
        with patch.object(module, 'run_command', return_value=(None,'','')):
            x=module.chrono_observation()
        self.assertEqual(x['method'],'unavailable')
    def test_chrony_normal_parsed(self):
        out='System time     : 0.000123456 seconds slow of NTP time\nStratum         : 3\nLeap status     : Normal\n'
        with patch.object(module, 'run_command', return_value=(0,out,'')):
            x=module.chrono_observation()
        self.assertEqual(x['stratum'],3)
        self.assertEqual(x['leap_status'],'Normal')
        self.assertAlmostEqual(x['system_offset_seconds'],-0.000123456)
    def test_chrony_not_synchronized(self):
        out='System time : 0.000000000 seconds fast of NTP time\nStratum: 0\nLeap status: Not synchronised\n'
        with patch.object(module, 'run_command', return_value=(0,out,'')):
            x=module.chrono_observation()
        self.assertEqual(x['leap_status'],'Not synchronised')
        self.assertEqual(x['stratum'],0)
    def test_mock_preflight_blocked_by_dns(self):
        with TemporaryDirectory() as d:
            x=module.snapshot(Path(d))
            with patch.object(module,'snapshot',return_value={**x,'dns_status':'FAILED_DNS_LOOKUP',
                     'clock': {'method':'unavailable'}}):
                pf=module.preflight(Path(d),'real_public_smoke_60s',1000000,2000000,0)
            self.assertIn('PUBLIC_COINBASE_DNS_UNAVAILABLE',pf['failures'])
            self.assertFalse(pf['ready_for_real_capture'])
    def test_invalid_resource_budgets(self):
        with TemporaryDirectory() as d:
            pf=module.preflight(Path(d),'real_public_smoke_60s',2000000,1000000,0)
            self.assertIn('INVALID_STORAGE_BUDGETS',pf['failures'])
    def test_preflight_only_creates_no_market_source(self):
        with TemporaryDirectory() as d:
            root=Path(d);work=root/'work';work.mkdir()
            evidence=root/'observer'
            args=[sys.executable,str(ROOT/'scripts/v25_capture_operator.py'),
                  '--workdir',str(work),'--evidence-dir',str(evidence),'--mode','smoke',
                  '--raw-budget','1048576','--archive-budget','2097152','--disk-reserve','0']
            cp=subprocess.run(args,text=True,capture_output=True,timeout=12)
            self.assertEqual(cp.returncode,2,cp.stderr)
            self.assertTrue((evidence/'V25_PREFLIGHT.json').is_file())
            self.assertFalse((work/'soak_evidence').exists())
            self.assertIn('PREFLIGHT_ONLY',cp.stdout)
    def test_execute_with_insufficient_storage_still_never_connects(self):
        with TemporaryDirectory() as d:
            root=Path(d);work=root/'work';work.mkdir()
            evidence=root/'observer'
            args=[sys.executable,str(ROOT/'scripts/v25_capture_operator.py'),
                  '--workdir',str(work),'--evidence-dir',str(evidence),
                  '--execute-real-public-capture','--mode','smoke',
                  '--raw-budget','1048576','--archive-budget','2097152','--disk-reserve','1000000000000']
            cp=subprocess.run(args,text=True,capture_output=True,timeout=12)
            self.assertNotEqual(cp.returncode,0)
            self.assertTrue((evidence/'V25_PREFLIGHT.json').is_file())
            self.assertFalse((work/'soak_evidence').exists())
    def test_evidence_overwrite_refused(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'a.json';module.exclusive_write(p,{'x':1})
            with self.assertRaises(FileExistsError):module.exclusive_write(p,{'x':2})
    def test_synthetic_flag_in_environment_cannot_bypass_preflight(self):
        with TemporaryDirectory() as d:
            root=Path(d);work=root/'work';work.mkdir()
            evidence=root/'observer'
            with patch.dict('os.environ',{'SOAK_TEST_FIXTURE':'1'}):
                args=[sys.executable,str(ROOT/'scripts/v25_capture_operator.py'),
                      '--workdir',str(work),'--evidence-dir',str(evidence),
                      '--mode','smoke','--raw-budget','1048576',
                      '--archive-budget','2097152','--disk-reserve','0']
                cp=subprocess.run(args,capture_output=True,text=True,timeout=12)
            self.assertEqual(cp.returncode,2)
            self.assertFalse((work/'soak_evidence').exists())
