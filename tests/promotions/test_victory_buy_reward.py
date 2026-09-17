"""Victory's buy-N-get-M-free and second-at-X% promotions (KAN-26).

Victory files these with the same nested <Groups> shape Super-Pharm and Fresh
Market use, but with its own twist: the "buy" leg's own DiscountedPrice is
not a total for MinQty, it is that barcode's shelf price at that branch - and
only when the branch actually stocks it. When it does not, Victory fills
near-zero junk (0, 0.01, 0.02) instead of leaving the field blank, and the
un-fixed code read that as a real price. The app's Discounts page showed a
₪0.01 bottle of Palmolive as a result.

The two group-level fixtures below (261418, 497180/261407) carry the exact
junk the ticket measured: the same barcode that is 0.02 on the buy leg is
also 0.01 on the reward leg, in the same file, at the same branch - proof
that neither leg's own DiscountedPrice can be trusted for a barcode the
branch does not stock.

Every fixture here is a real promotion, carved out of a real Victory dump
(chain 7290696200003, branch 097) with its header intact - see
fixtures/README.md. Nothing is invented.

The shapes covered, and why each is here:

  * victory_buy_two_get_one_free - tehina, promo 260963. The branch stocks
    the barcode (14.90), so this is the "trust it" half of the bug: the
    buy leg's own price happens to be right, but the code must not rely on
    that - it has to come out the same way through the real shelf price.

  * victory_gift_not_stocked - Palmolive, promo 261418, 15 interchangeable
    barcodes per group. Branch 097 stocks neither of the two checked here,
    so both buy legs are junk (0.02) - this is the actual KAN-26 bug: a
    ₪0.01/0.02 row must not be published at all.

  * victory_buy_one_get_one_free - peanut butter, promo 260950. The (1, 1)
    case: N and M both 1, still resolved from the branch's real shelf price.

  * victory_second_at_half_price - a deodorant stick, promo 261407. Not a
    gift: DiscountRate is 50 and the reward leg names a real (if untrusted)
    half price. Same arithmetic, generalised: unit_price = shelf * (N +
    M*(1-rate/100)) / (N+M).

  * victory_single_group_discount - one group, one item, DiscountRate
    -49400 (a real value pulled from this dump: garbage outside 2+1/second-
    at-X% pairs is common in Victory's dialect and must never be read). The
    ordinary shape most Victory promotions have - a regression guard.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import build_chain_db  # noqa: E402
import promos  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CHAIN = "7290696200003"

TEHINA = "7290010328253"
PALMOLIVE_A = "8714789733296"
PALMOLIVE_B = "8718951649613"
PEANUT_BUTTER = "7290019926573"
DEODORANT = "7290004078942"
BUTTER = "7290108507997"


def offers_for(scenario, barcode):
    """Every offer one scenario yields for one barcode, as in test_multi_group_offers."""
    return [offer._asdict()
            for _store_id, offer in promos.read_offers(os.path.join(FIXTURES, scenario))
            if offer.barcode == barcode]


class TestBuyTwoGetOneFree:
    """Tehina, promo 260963: N=2, M=1, the branch stocks the barcode at 14.90."""

    def test_the_structure_is_read_as_unpriced(self):
        """The buy leg's 14.90 is not trusted directly, even though it is right.

        min_qty is the full 3 items (2 bought + 1 free); known_price is 0
        because neither leg's own price is used, and unpriced_qty carries the
        weighted N + M*(1-rate/100) = 2 + 1*0 = 2 that resolve_unpriced_offers
        will multiply by the branch's real shelf price.
        """
        offer = offers_for("victory_buy_two_get_one_free", TEHINA)[0]
        assert offer["price"] is None
        assert offer["min_qty"] == 3.0
        assert offer["known_price"] == 0.0
        assert offer["unpriced_qty"] == 2.0

    def test_resolved_against_the_branchs_own_price(self):
        """2 * 14.90 / 3 = 9.93, the number the ticket measured."""
        conn = _db_with_price(TEHINA, 14.90)
        _resolve(conn, "victory_buy_two_get_one_free")
        assert _rows(conn) == [(TEHINA, 3.0, 29.80, 9.93)]


class TestGiftLegNotStocked:
    """Palmolive, promo 261418: both checked barcodes are junk at branch 097."""

    def test_a_barcode_the_branch_does_not_stock_is_dropped(self):
        """No shelf price, no total - the row must not surface at all, and
        specifically not at the junk 0.01/0.02 the buy leg states."""
        conn = _db_with_price(PALMOLIVE_A, None)
        _resolve(conn, "victory_gift_not_stocked")
        assert _rows(conn) == []

    def test_the_junk_price_never_reaches_promo_offers(self):
        """Regression guard for the specific bug: even if a caller forgot to
        drop unpriced offers, the parsed offer itself carries no price to
        publish - price is None, not 0.01 or 0.02."""
        for barcode in (PALMOLIVE_A, PALMOLIVE_B):
            offer = offers_for("victory_gift_not_stocked", barcode)[0]
            assert offer["price"] is None
            assert offer["unit_price"] is None

    def test_a_branch_that_does_stock_it_is_unaffected_by_the_other(self):
        """One barcode's junk buy leg must not sink the sibling barcode that
        does have a real shelf price - each of the 15 interchangeable codes
        in this group resolves on its own."""
        conn = _db_with_price(PALMOLIVE_A, None)
        conn.execute("INSERT INTO chain_prices VALUES (?,?,?,?)",
                     (CHAIN, PALMOLIVE_B, 23.90, 40))
        conn.commit()
        _resolve(conn, "victory_gift_not_stocked")
        assert _rows(conn) == [(PALMOLIVE_B, 3.0, 47.80, 15.93)]


class TestBuyOneGetOneFree:
    """Peanut butter, promo 260950: the (1, 1) case."""

    def test_structure(self):
        offer = offers_for("victory_buy_one_get_one_free", PEANUT_BUTTER)[0]
        assert offer["min_qty"] == 2.0
        assert offer["known_price"] == 0.0
        assert offer["unpriced_qty"] == 1.0

    def test_resolved_price(self):
        conn = _db_with_price(PEANUT_BUTTER, 14.90)
        _resolve(conn, "victory_buy_one_get_one_free")
        assert _rows(conn) == [(PEANUT_BUTTER, 2.0, 14.90, 7.45)]


class TestSecondAtHalfPrice:
    """Deodorant, promo 261407: nothing is free, DiscountRate is 50.

    The reward leg names its own half price (10.95, right when the branch
    stocks the 21.90 stick) but it is not read - only the rate is. Resolving
    against the branch's real 21.90 gives the same answer here, and does not
    when the branch does not stock the item, exactly like the gift shape.
    """

    def test_the_rate_is_read_not_the_reward_legs_own_price(self):
        offer = offers_for("victory_second_at_half_price", DEODORANT)[0]
        assert offer["price"] is None
        assert offer["min_qty"] == 2.0
        assert offer["known_price"] == 0.0
        assert offer["unpriced_qty"] == 1.5   # 1 + 1*(1 - 50/100)

    def test_resolved_price(self):
        """21.90 + 21.90*0.5 = 32.85 for two, 16.43 each."""
        conn = _db_with_price(DEODORANT, 21.90)
        _resolve(conn, "victory_second_at_half_price")
        assert _rows(conn) == [(DEODORANT, 2.0, 32.85, 16.43)]


class TestOrdinaryVictoryOfferIsUntouched:
    """The common case: one group, one item - most Victory promotions look
    like this, and 258803 carries a DiscountRate of -49400 to prove the new
    leg-pairing logic never even looks at it outside a real buy/reward pair.
    """

    def test_a_single_group_offer_reads_as_before(self):
        rows = offers_for("victory_single_group_discount", BUTTER)
        assert len(rows) == 1
        offer = rows[0]
        assert offer["min_qty"] == 1.0
        assert offer["price"] == 9.9
        assert offer["unit_price"] == 9.9
        assert offer["known_price"] == 9.9
        assert offer["unpriced_qty"] == 0.0


def _db_with_price(barcode, price):
    conn = sqlite3.connect(":memory:")
    conn.executescript(build_chain_db.SCHEMA)
    conn.execute("INSERT INTO chains VALUES (?,?)", (CHAIN, "ויקטורי"))
    if price is not None:
        conn.execute("INSERT INTO chain_prices VALUES (?,?,?,?)",
                     (CHAIN, barcode, price, 40))
    conn.commit()
    return conn


def _resolve(conn, scenario, store_id="97"):
    staged = [
        (offer.chain_id, offer.promo_id, offer.barcode, offer.club, offer.coupon,
         store_id, offer.min_qty, offer.known_price, offer.unpriced_qty,
         offer.description, offer.starts, offer.ends)
        for _sid, offer in promos.read_offers(os.path.join(FIXTURES, scenario))
        if offer.unpriced_qty
    ]
    return build_chain_db.resolve_unpriced_offers(conn, staged)


def _rows(conn):
    return conn.execute(
        "SELECT barcode, min_qty, price, unit_price FROM promo_offers "
        "ORDER BY barcode").fetchall()
