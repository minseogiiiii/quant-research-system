"""Strict memory envelope tests; all inputs synthetic."""
import unittest

from q299d_recurrent_burst import CYCLES, MAX_RSS_DRIFT, memory_contract


class RecurrentMemoryContract(unittest.TestCase):
    def test_exact_drift_boundary_and_warmup(self):
        base = 600 * 1024**2
        rows = [[base]*20 for _ in range(CYCLES)]
        rows[-1] = [base+MAX_RSS_DRIFT]*20
        self.assertTrue(memory_contract(rows)["passed"])
        rows[-1] = [base+MAX_RSS_DRIFT+1]*20
        self.assertFalse(memory_contract(rows)["passed"])

    def test_incomplete_cycles_and_samples(self):
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_MEMORY_SAMPLES"):
            memory_contract([[1]*20 for _ in range(CYCLES-1)])
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_MEMORY_SAMPLES"):
            memory_contract([[1]*19 for _ in range(CYCLES)])


if __name__ == "__main__":
    unittest.main()
