import unittest

from q293_c1_dual_18000s import DURATION, MIN_FREE, RSS_LIMIT
from q293_long_policy import GIB, admission, budgets


class C1Contract(unittest.TestCase):
    def test_strict_five_hour_budget(self):
        self.assertEqual(DURATION, 18000)
        self.assertEqual(MIN_FREE, admission(18000))
        self.assertEqual(RSS_LIMIT, 3 * GIB)
        self.assertEqual(budgets(DURATION), (12600000000, 16200000000))
        self.assertGreaterEqual(MIN_FREE, 32400000000 + 6 * GIB)
