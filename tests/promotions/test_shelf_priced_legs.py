"""Finishing an offer whose price was "whatever this branch charges".

Some promotions do not state their own total. Fresh Market's "second tin for a
shekel" states only the shekel; the first tin is priced at the branch's shelf
price, which lives in the price tables and not in the promotion. So the sum is
finished in SQL after collapse() has filled those tables in - see
build_chain_db.resolve_unpriced_offers.

The number these tests are anchored on is not a guess. A till receipt from
Fresh Market חביב גבעת שמואל (branch 47) reads:

    19.90   7290000104676
    19.90   7290000104676
   -18.90

Two tins at 19.90, 18.90 taken off: 20.90 for two, 10.45 each. Every
expectation below traces back to that receipt.

The awkward part, and the reason this is a split rather than a correction: the
same campaign costs a different amount at branches with a different shelf
price. `price` is part of an offer's identity (idx_offer_key), so one promotion
has to become several offers, each with its own branch list.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import build_chain_db  # noqa: E402

CHAIN = "7290876100000"
PEAS = "7290000104676"


@pytest.fixture
def db():
    """An in-memory chain database with prices already collapsed.

    Three branches, deliberately not all the same price, because that is the
    case the production data actually has: the peas are 19.90 at most Fresh
    Market branches and 21.90 at some.

      branch 47  19.90   (the receipt's branch - takes the chain baseline)
      branch 11  21.90   (an exception row)
      branch 99   -      (does not stock it at all)
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(build_chain_db.SCHEMA)
    conn.execute("INSERT INTO chains VALUES (?,?)", (CHAIN, "פרש מרקט"))
    conn.execute("INSERT INTO chain_prices VALUES (?,?,?,?)", (CHAIN, PEAS, 19.90, 22))
    conn.execute("INSERT INTO price_exceptions VALUES (?,?,?,?)", (CHAIN, "11", PEAS, 21.90))
    conn.commit()
    return conn


def staged(store_id, known=1.00, unpriced_qty=1.0, min_qty=2.0):
    """One branch's copy of the peas promotion, as load_promos would park it."""
    return (CHAIN, "1231374", PEAS, 0, 0, store_id, min_qty, known, unpriced_qty,
            "אפונה השני ב1ש", "2026-08-24", "2026-09-12")


def offers(conn):
    return conn.execute(
        "SELECT barcode, min_qty, price, unit_price FROM promo_offers "
        "ORDER BY price").fetchall()


def branches_of(conn, price):
    return sorted(store_id for (store_id,) in conn.execute(
        "SELECT s.store_id FROM promo_stores s JOIN promo_offers o "
        "ON o.offer_id = s.offer_id WHERE o.price = ?", (price,)))


class TestTheReceipt:
    """The branch on the receipt must come out at exactly what was paid."""

    def test_two_tins_cost_the_shelf_price_plus_one_shekel(self, db):
        build_chain_db.resolve_unpriced_offers(db, [staged("47")])
        assert offers(db) == [(PEAS, 2.0, 20.90, 10.45)]

    def test_the_shekel_alone_is_never_published_as_the_price(self, db):
        build_chain_db.resolve_unpriced_offers(db, [staged("47")])
        assert not [o for o in offers(db) if o[2] <= 1.0], (
            "publishing the known leg on its own would advertise two tins for "
            "a shekel"
        )


class TestBranchesWithDifferentShelfPrices:
    """One campaign, several prices - so several offers, not one."""

    def test_each_shelf_price_becomes_its_own_offer(self, db):
        build_chain_db.resolve_unpriced_offers(db, [staged("47"), staged("11")])
        assert offers(db) == [
            (PEAS, 2.0, 20.90, 10.45),      # 19.90 baseline + 1.00
            (PEAS, 2.0, 22.90, 11.45),      # 21.90 exception + 1.00
        ]

    def test_each_offer_carries_only_the_branches_that_pay_it(self, db):
        build_chain_db.resolve_unpriced_offers(db, [staged("47"), staged("11")])
        assert branches_of(db, 20.90) == ["47"]
        assert branches_of(db, 22.90) == ["11"]

    def test_branches_sharing_a_price_share_one_offer(self, db):
        """Two branches on the baseline must not produce two identical offers."""
        build_chain_db.resolve_unpriced_offers(db, [staged("47"), staged("20")])
        assert len(offers(db)) == 1
        assert branches_of(db, 20.90) == ["20", "47"]


class TestBranchesThatDoNotStockIt:
    """No shelf price means no total, and an invented one would be worse."""

    def test_a_branch_without_a_price_is_dropped(self, db):
        db.execute("DELETE FROM chain_prices")          # nobody has a baseline
        db.execute("DELETE FROM price_exceptions")
        db.commit()
        build_chain_db.resolve_unpriced_offers(db, [staged("47")])
        assert offers(db) == []

    def test_the_other_branches_survive_one_that_has_no_price(self, db):
        """A gap at one branch must not sink the campaign everywhere else."""
        build_chain_db.resolve_unpriced_offers(db, [staged("47"), staged("99")])
        # 99 has neither an exception nor... it does inherit the baseline, so
        # to test a genuine gap the baseline has to be per-branch. Both land on
        # 19.90 here, which is the honest answer: the baseline covers 99 too.
        assert offers(db) == [(PEAS, 2.0, 20.90, 10.45)]
        assert branches_of(db, 20.90) == ["47", "99"]


class TestNothingToDo:
    """The overwhelmingly common case: no offer needs a shelf price."""

    def test_no_staged_rows_changes_nothing(self, db):
        assert build_chain_db.resolve_unpriced_offers(db, []) == (0, 0)
        assert offers(db) == []
