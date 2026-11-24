import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path
from h2_tardis_l2 import convert,HEADER,day_bounds
DAY="2024-01-01";T=day_bounds(DAY)[0]
def row(t,flag,side,p,q,venue=None):
    return f"coinbase,BTC-USD,{t if venue is None else venue},{t},{flag},{side},{p},{q}\n"
def write(root,rows):
    path=root/"vendor.csv.gz"
    with gzip.open(path,"wt",encoding="ascii",newline="") as f:
        f.write(",".join(HEADER)+"\n");f.writelines(rows)
    return path,hashlib.sha256(path.read_bytes()).hexdigest()
def fixture():
    return [row(T+1,"false","bid","1","1"),row(T+2,"true","bid","99","2"),
        row(T+2,"true","ask","101","3"),row(T+3,"false","bid","99","0"),
        row(T+3,"false","bid","98","1.000000000000000001",T+1),
        row(T+4,"true","bid","200","1"),row(T+4,"true","ask","201","2"),
        row(T+5,"false","ask","201","3")]
class Vendor(unittest.TestCase):
    def test_two_epochs_no_stitch(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src,pin=write(root,fixture())
            a=convert(src,pin,root/"out",symbol="BTC-USD",day=DAY,fixture=True)
            self.assertEqual(a["pre_snapshot_rows_skipped"],1)
            self.assertEqual(a["admitted_rows"],7)
            self.assertEqual(len(a["epochs"]),2)
            self.assertEqual(a["groups"],4)
            self.assertTrue(a["independent_final_epoch_book_parity"])
            self.assertFalse(a["native_sequence_available"])
            self.assertFalse(a["research_eligible"])
    def test_reject_crossed_bad_pin_or_regression(self):
        for rows,bad in [(fixture(),True),(fixture()+[row(T+3,"false","bid","200","1")],False),
                         ([row(T+2,"true","bid","101","1"),row(T+2,"true","ask","100","1")],False)]:
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);src,pin=write(root,rows)
                with self.assertRaises(ValueError):convert(src,"0"*64 if bad else pin,root/"out",symbol="BTC-USD",day=DAY,fixture=True)
                self.assertFalse((root/"out").exists())
    def test_snapshot_across_time_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src,pin=write(root,[row(T+2,"true","bid","99","1"),row(T+3,"true","ask","101","1")])
            with self.assertRaisesRegex(ValueError,"MULTI_TIMESTAMP"):convert(src,pin,root/"out",symbol="BTC-USD",day=DAY,fixture=True)
