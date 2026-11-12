"""No-arbitrage complement consistency of the dual token books.

Up and Down redeem to $1.00 together. The venue feeds two independent book
snapshots that momentarily disagree near expiry; before any decision or fill
the runner must reconcile them so a taker can never buy both asks for < $1.00.
"""
import unittest

from core.types import OrderBook
from sim.matcher import make_complementary


def book(bids, asks):
    return OrderBook(bids=bids, asks=asks, source_t=1.0, received_at=1.0)


class TestComplementConsistency(unittest.TestCase):
    def test_inconsistent_asks_are_floored_to_one(self):
        up = book([(0.66, 5)], [(0.67, 5)])
        dn = book([(0.26, 5)], [(0.27, 5)])
        self.assertLess(up.best_ask + dn.best_ask, 1.0)
        cu, cd = make_complementary(up, dn)
        self.assertGreaterEqual(cu.best_ask + cd.best_ask, 1.0 - 1e-9)

    def test_inconsistent_bids_are_capped_to_one(self):
        up = book([(0.60, 5)], [(0.62, 5)])
        dn = book([(0.55, 5)], [(0.57, 5)])
        self.assertGreater(up.best_bid + dn.best_bid, 1.0)
        cu, cd = make_complementary(up, dn)
        self.assertLessEqual(cu.best_bid + cd.best_bid, 1.0 + 1e-9)

    def test_consistent_books_unchanged(self):
        up = book([(0.44, 5)], [(0.45, 5)])
        dn = book([(0.54, 5)], [(0.55, 5)])
        cu, cd = make_complementary(up, dn)
        self.assertIs(cu, up)
        self.assertIs(cd, dn)

    def test_legitimate_post_move_lock_preserved(self):
        # Down bought cheap earlier, market moved, Up now cheap but the two
        # current books are consistent (sum == 1). This real arbitrage lock must
        # survive untouched.
        up = book([(0.27, 5)], [(0.28, 5)])
        dn = book([(0.71, 5)], [(0.72, 5)])
        cu, cd = make_complementary(up, dn)
        self.assertIs(cu, up)
        self.assertIs(cd, dn)

    def test_depth_and_metadata_preserved(self):
        up = book([(0.66, 5), (0.65, 10)], [(0.67, 5), (0.68, 10)])
        dn = book([(0.26, 5)], [(0.27, 5), (0.28, 10)])
        cu, cd = make_complementary(up, dn)
        self.assertEqual([s for _, s in cu.asks], [5, 10])
        self.assertEqual([s for _, s in cd.asks], [5, 10])
        self.assertEqual(cu.source_t, 1.0)
        # every ask level pair stays no-arbitrage after the shift
        self.assertGreaterEqual(cu.asks[1][0] + cd.asks[1][0], 1.0 - 1e-9)

    def test_empty_side_left_alone(self):
        up = book([(0.44, 5)], [(0.45, 5)])
        dn = book([], [])
        cu, cd = make_complementary(up, dn)
        self.assertIs(cu, up)
        self.assertIs(cd, dn)


if __name__ == "__main__":
    unittest.main()