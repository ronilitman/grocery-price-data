"""What a promotion built from several <Group>s should turn into.

A promotion is not always one offer with one price. The chains express
"buy one, the second is a shekel" as a single <Promotion> holding several
<Group> children, and satisfying the deal means taking one item from each of
them. Read the groups apart and the shekel looks like the price of a bottle of
mouthwash, which is exactly what the app once showed.

Every fixture here is a real promotion, carved out of a real dump with its
header intact - see fixtures/README.md. Nothing in this directory is invented,
because the whole class of bug being guarded against is "the file did not mean
what we assumed".

The three shapes that matter, and why each is here:

  * super_pharm_second_at_one_shekel - two groups, both carrying a price. The
    simple case, and the one the current code handles.

  * fresh_market_second_at_one_shekel - three groups: a spend threshold, a
    qualifying group with NO price, and the shekel. The threshold is not a
    product and the blank price means "at whatever this branch charges". This
    is the shape that silently lost a real discount at 46 branches.

  * super_pharm_gift_with_purchase - two groups holding different products.
    Buying a perfume gets you a free miniature; neither item's own price
    changes, so there is nothing honest to publish and it must be dropped.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import promos  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

LISTERINE = "7290008116565"
PEAS = "7290000104676"


def offers_for(scenario, barcode):
    """Every offer one scenario yields for one barcode.

    Each scenario is its own directory holding a realistically named
    PromoFull dump, so read_offers() walks it exactly as it walks a real
    chain's dumps - filename conventions included.
    """
    return [offer._asdict()
            for _store_id, offer in promos.read_offers(os.path.join(FIXTURES, scenario))
            if offer.barcode == barcode]


class TestTwoGroupsBothPriced:
    """Super-Pharm: one bottle at 28.90, the second at 1.00.

    Both groups state a price, so the whole deal can be read from the
    promotion alone - no shelf price needed.
    """

    def test_the_two_groups_become_one_offer(self):
        rows = offers_for("super_pharm_second_at_one_shekel", LISTERINE)
        assert len(rows) == 1, (
            "the two groups are two halves of one deal, not two offers; "
            "yielding both is what advertised a 1.00 bottle of Listerine"
        )

    def test_quantity_and_total_come_from_summing_the_groups(self):
        offer = offers_for("super_pharm_second_at_one_shekel", LISTERINE)[0]
        assert offer["min_qty"] == 2.0        # one bottle from each group
        assert offer["price"] == 29.90        # 28.90 + 1.00

    def test_unit_price_is_the_total_divided_by_the_quantity(self):
        offer = offers_for("super_pharm_second_at_one_shekel", LISTERINE)[0]
        assert offer["unit_price"] == 14.95

    def test_the_shekel_leg_never_escapes_on_its_own(self):
        """The specific regression: no offer may claim one bottle costs 1.00."""
        rows = offers_for("super_pharm_second_at_one_shekel", LISTERINE)
        assert not [r for r in rows if r["unit_price"] <= 1.0], (
            "a lone 1.00 here is the second bottle's price masquerading as the "
            "product's price"
        )


class TestGroupsHoldingDifferentProducts:
    """Buy a Cerruti perfume, get a 9.5 ml miniature free.

    The groups do not overlap: nothing is bought twice, and neither item's own
    price changes. Group one's 221.00 is what the perfume costs anyway, so
    publishing it would invent a discount that does not exist.
    """

    def test_a_cross_product_gift_is_not_priced(self):
        perfume = "5050456006663"       # in the qualifying group, at 221.00
        gift = "5050456007929"          # in the reward group, at 0.00
        assert offers_for("super_pharm_gift_with_purchase", perfume) == []
        assert offers_for("super_pharm_gift_with_purchase", gift) == []


class TestThresholdGroupAndAnUnpricedLeg:
    """Fresh Market: spend 75, then one tin at shelf price and one at 1.00.

    Three groups, and only one of them is a plain product list:

        group 1   MinPurchaseAmount 75.00, item 0000000000000
        group 2   four real barcodes, no price at all
        group 3   the same four barcodes, 1.00

    Group one is a condition, not a leg - its "item" is a placeholder that no
    shopper can buy. Group two's empty price is not zero and not free: it means
    the tin costs whatever that branch charges for it.

    A till receipt from branch 47 settles what the answer must be: two tins
    rang up at 19.90 each with 18.90 taken off, so 20.90 for two.
    """

    def test_the_promotion_is_not_discarded(self):
        rows = offers_for("fresh_market_second_at_one_shekel", PEAS)
        assert rows, (
            "requiring the barcode in every group throws this away, because it "
            "can never appear in a spend threshold whose only item is a "
            "placeholder"
        )

    def test_the_threshold_group_is_not_counted_as_a_leg(self):
        offer = offers_for("fresh_market_second_at_one_shekel", PEAS)[0]
        assert offer["min_qty"] == 2.0, (
            "two tins, not three - the 75 shekel threshold is a condition on "
            "the basket, not another tin to buy"
        )

    def test_the_priced_leg_is_carried_through(self):
        """The shekel is known here; the shelf price is resolved later in SQL."""
        offer = offers_for("fresh_market_second_at_one_shekel", PEAS)[0]
        assert offer["known_price"] == 1.00
        assert offer["unpriced_qty"] == 1.0

    def test_the_offer_is_marked_as_needing_a_shelf_price(self):
        offer = offers_for("fresh_market_second_at_one_shekel", PEAS)[0]
        assert offer["unpriced_qty"] > 0, (
            "without this marker the offer would be published at 1.00 for two, "
            "which is the Super-Pharm bug wearing a different hat"
        )


class TestOrdinaryOffersAreUntouched:
    """The common case: one group, one price. Most promotions look like this."""

    def test_a_single_group_offer_reads_as_before(self):
        rows = offers_for("super_pharm_single_group_discount", "7290104965401")
        assert len(rows) == 1
        offer = rows[0]
        assert offer["min_qty"] == 1.0
        assert offer["price"] == 29.90
        assert offer["unit_price"] == 29.90
        assert offer["coupon"] == 1, "AdditionalIsCoupon is set on this one"
