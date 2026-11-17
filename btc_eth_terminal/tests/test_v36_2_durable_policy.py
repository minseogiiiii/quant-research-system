"""Strict durable policy unit-only, not a real persistent Linux host test."""
import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v36_observe_existing_collector import durable_policy_valid

class StrictDurablePolicy(unittest.TestCase):
 def test_day_policy(self):
  self.assertTrue(durable_policy_valid(86400,40_000_000_000,55_000_000_000,10*1024**3))
 def test_short_duration_rejected(self):
  self.assertFalse(durable_policy_valid(3599,40_000_000_000,55_000_000_000,10*1024**3))
 def test_more_than_day_rejected(self):
  self.assertFalse(durable_policy_valid(86401,40_000_000_000,55_000_000_000,10*1024**3))
 def test_raw_budget_cannot_be_smaller(self):
  self.assertFalse(durable_policy_valid(86400,20_000_000_000,55_000_000_000,10*1024**3))
 def test_archive_budget_cannot_be_smaller(self):
  self.assertFalse(durable_policy_valid(86400,40_000_000_000,50_000_000_000,10*1024**3))
 def test_reserved_space_cannot_be_smaller(self):
  self.assertFalse(durable_policy_valid(86400,40_000_000_000,55_000_000_000,4*1024**3))
 def test_boolean_budget_is_invalid(self):
  self.assertFalse(durable_policy_valid(86400,True,55_000_000_000,10*1024**3))
 def test_archive_budget_must_exceed_raw_budget(self):
  self.assertFalse(durable_policy_valid(3600,60_000_000_000,55_000_000_000,10*1024**3))
if __name__=='__main__':unittest.main()
