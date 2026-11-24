import unittest
from unittest.mock import patch
from f4_depth_buckets import project, units, decimal, build


def level(side, px, qty):
    return {"side": side, "price_level": px, "new_quantity": qty}


def ev(t, seq, kind, updates):
    return (t, seq, "BTC-USD", kind, updates)


OPTIONS = dict(product="BTC-USD", epoch="a"*64+":0", start_ms=1000,
               end_ms=5000, bucket_ms=1000, low="90", high="120", width="10")
SNAP = ev(1100, 1, "snapshot", [level("bid","100","0.1"),level("bid","101","0.2"),level("offer","110","3")])


class Buckets(unittest.TestCase):
    def test_exact_quantity_last_observation_and_delete(self):
        result = project([SNAP, ev(1500,2,"update",[level("bid","101","0.4")]),
                          ev(3100,4,"update",[level("bid","101","0")])], **OPTIONS)
        first, last = result["rows"]
        self.assertEqual(first["observed_receiver_ms"],1500)
        self.assertEqual(first["cells"][0]["quantity"],"0.5")
        self.assertEqual(last["cells"][0]["quantity"],"0.1")
        self.assertEqual(result["missing_buckets_ms"],[2000,4000])
        self.assertFalse(result["zero_gap_certified"])

    def test_half_open_time_and_price(self):
        r = project([SNAP, ev(2000,2,"update",[level("offer","120","8")]),
                     ev(5000,3,"update",[level("bid","100","9")])], **OPTIONS)
        self.assertEqual(len(r["rows"]),2)
        self.assertEqual(r["rows"][1]["bucket_start_ms"],2000)
        self.assertNotIn("120",[c["price_low"] for c in r["rows"][1]["cells"]])
        self.assertEqual(r["rows"][1]["cells"][0]["quantity"],"0.3")

    def test_no_snapshot_or_duplicate_or_regression(self):
        for rows in ([ev(1100,1,"update",[])],[SNAP,SNAP],
                     [SNAP,ev(1000,2,"update",[])],[SNAP,ev(1200,0,"update",[])]):
            with self.assertRaises(ValueError):
                project(rows, **OPTIONS)

    def test_decimal_exactness_and_rejections(self):
        self.assertEqual(decimal(units("0.000000000000000001")),"0.000000000000000001")
        for raw in ("NaN","1e-3","-1","0.0000000000000000001"):
            with self.assertRaises(ValueError):
                units(raw)

    def test_epoch_gate_before_replay(self):
        with patch("f4_depth_buckets.verify_capture",return_value={"source_eligible_for_state_replay":True,"connections":2}):
            with self.assertRaisesRegex(ValueError,"SINGLE_EPOCH"):
                build(".", "a"*64)

    def test_pre_window_snapshot_without_fabricated_bucket(self):
        r=project([ev(900,1,"snapshot",SNAP[-1]),ev(2200,2,"update",[])],**OPTIONS)
        self.assertEqual([x["bucket_start_ms"] for x in r["rows"]],[2000])
        self.assertIn(1000,r["missing_buckets_ms"])


if __name__ == "__main__":
    unittest.main()
