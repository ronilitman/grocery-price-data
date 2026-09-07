"""PromoFull XML -> one row per distinct offer, whatever dialect the branch speaks.

Promotions are read straight from the downloaded XML rather than from the
parser package's CSV, for two reasons.

**The CSV cannot express them.** A promotion nests an item list, and the
generic converter flattens that to a JSON blob in a column whose name depends
on the dialect. The old loader looked for a column containing "item", which
matched one dialect and silently skipped the other - so Yellow shipped 1 of its
244 branches and Stop Market shipped none of its 11.

**The CSV is enormous.** Yellow's promo CSV was 1,970 MB against 76 MB for its
prices, because every branch republishes the whole chain's promotions and the
writer expands each one per item. Nothing here ever writes that file.

Two dialects are in the wild, and a single chain can publish both - Keshet has
24 branches on one and 2 on the other, emitting the same promotion id with
different ``RewardType`` values. So ``RewardType``, ``IsGiftItem`` and
``AllowMultipleDiscounts`` are all dialect artefacts and none of them are read
here. What survives the dialect intact is the offer itself:

    PromotionId, PromotionDescription, MinQty, DiscountedPrice, ClubId,
    AdditionalIsCoupon, dates

  nested <Groups>                      flat <PromotionItems>
    <Promotion>                          <Promotion>
      <ClubID>                             <MinQty> <DiscountedPrice>
      <Groups><Group><PromotionItems>      <Clubs><ClubId>
        <PromotionItem>                    <PromotionItems>
          <ItemCode>                         <Item><ItemCode>
          <MinQty> <DiscountedPrice>

``<Group>`` is the exception to that: it is structure, not dialect. Two groups
mean one deal with two halves - "buy one at 28.90, the second at 1.00" - and
flattening them into two offers is how a 1 shekel Listerine reached the app.
See ``_across_groups``.

The unit price is computed here as ``DiscountedPrice / MinQty`` and never taken
from ``DiscountedPricePerMida``: that field is per *measure* unit, so Stop
Market publishes 8.80 for a 2-for-22 deal on a 125 g box (22 / 2.5 hundred
grams). CHP does the same division, which is how their page shows 11.00 for
that offer.

That division is wrong for exactly one case, and it is a big one. A MinQty
below 1 is not a pack size - nobody buys a hundredth of a box - it is a
*weight*, and the chains use it to say "this is the kilo price". Rami Levy
files its 2.90 tomato promotion as MinQty 0.01 with DiscountedPrice 2.90, so
dividing turns a real 2.90/kg discount into 290.00 a kilo. 15,289 offers carry
a fractional MinQty and on Shufersal every single one is a weighed product, so
this is the whole country's fresh-produce discounts, not an edge case. Below 1,
the discounted price IS the unit price and there is no minimum count to meet.
"""

import os
import xml.etree.ElementTree as ET
from typing import NamedTuple

# Above this, MinQty is not a pack size but a spend threshold: Yellow's
# "5בום חודש יולי 100- שח פריטי אוסם" carries MinQty=10000 against a basket
# price of 100, which divides out to one agora a packet. Basket promotions are
# real, but they are not a price for the item and cannot be shown as one.
MAX_MIN_QTY = 50

# Loyalty blankets - "צבירה ביילו אשראי 10%", "הנחת 5%" - carry no
# DiscountedPrice and cover the entire catalogue: six of them accounted for
# 22,784 of Yellow's 25,189 old rows. Dropping the unpriced ones is what makes
# the table small enough to publish.
MIN_PRICE = 0.01


class Offer(NamedTuple):
    """One promotion, as one branch published it.

    A NamedTuple rather than a bare tuple because the row grew past the point
    where positional unpacking was readable, and because two consumers unpack
    it - build_chain_db.py and the tests.

    ``price`` and ``unit_price`` are None only while an offer is waiting on a
    shelf price it could not know: one of its legs said "at whatever this
    branch charges". ``known_price`` is what the other legs came to and
    ``unpriced_qty`` how much still needs pricing, so build_chain_db.py can
    finish the sum against the branch's own price.
    """

    chain_id: str
    promo_id: str
    barcode: str
    club: int
    coupon: int
    min_qty: float
    price: float
    unit_price: float
    description: str
    starts: str
    ends: str
    known_price: float = 0.0
    unpriced_qty: float = 0.0


def _text(node, *names):
    for name in names:
        value = node.findtext(name)
        if value not in (None, ""):
            return value.strip()
    return ""


def _number(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def club_of(promotion):
    """0 if anyone can have the price, 1 if it needs a loyalty card.

    ``ClubID`` is free text and chains fill it with anything: ``0``, ``1=``,
    ``1=Yellow friends``, ``1=מועדון שוטרים``, ``(1=test)&(3=פז סנדביץ)``.
    Enumerating the clubs is a losing game and the app only needs to know
    whether a card is required, so the test is the leading code: 0 means
    everyone, anything else means a card.
    """
    raw = _text(promotion, "ClubID", "ClubId")
    if not raw:
        clubs = promotion.find("Clubs")
        if clubs is not None:
            raw = "".join((child.text or "") for child in clubs).strip()
    code = (raw or "0").lstrip("(").split("=")[0].strip()
    return (0 if code in ("", "0") else 1), raw


def coupon_of(promotion):
    """1 if the price needs a coupon claimed in the chain's app, 0 otherwise.

    A coupon is not a shelf price. Super-Pharm's "קופון לייף בייביז משטחי
    החתלה" is 12.90 against a 17.90 shelf, but only after the shopper adds the
    coupon in the app, and only once. Sold as an ordinary discount it is a
    number most baskets will not get, which is why CHP and Pricez leave these
    out of their comparisons entirely.

    ``AdditionalIsCoupon`` is a standard field and the chains do fill it in:
    every one of Super-Pharm's 1,062 promotions per branch carries it, 72 of
    them set. Do not infer this from the description instead - a sample of one
    Shufersal file had 102 coupons against 92 whose text says "קופון", and Tiv
    Taam had two ordinary discounts whose text says it and whose flag does not.

    Absent, as it may be in the flat dialect, reads as 0: not knowing must look
    like an ordinary discount, or a chain that omits the field loses its
    promotions rather than merely losing the distinction.
    """
    raw = _text(promotion, "AdditionalIsCoupon", "AdditionalIsCupon")
    return 1 if raw.strip() in ("1", "true", "True") else 0


# A group whose only "item" is this placeholder is not a product list. Fresh
# Market uses it to express a spend threshold - "MinPurchaseAmount 75.00, item
# 0000000000000" - and no shopper can put that in a basket.
PLACEHOLDER_ITEM = "0000000000000"


def _group_terms(group):
    """``{barcode: (min_qty, price)}`` for one ``<Group>``.

    ``price`` is None when the group states none. That is not zero and not
    free: it means the item costs whatever the branch charges for it, which
    only the price tables know. See ``_across_groups``.
    """
    terms = {}
    for item in group.iter("PromotionItem"):
        code = _text(item, "ItemCode")
        if code:
            terms[code] = (_number(_text(item, "MinQty")),
                           _number(_text(item, "DiscountedPrice")))
    return terms


def _is_threshold(terms):
    """A group that states a condition on the basket rather than a leg to buy."""
    return set(terms) <= {PLACEHOLDER_ITEM}


def _legs(groups):
    """The groups that are actually legs of the deal, thresholds discarded."""
    return [terms for terms in (_group_terms(g) for g in groups)
            if not _is_threshold(terms)]


def _across_legs(legs):
    """One combined ``(barcode, qty, price, known, unpriced_qty)`` per barcode.

    A promotion with several ``<Group>``s is not several offers, it is one
    offer with several legs that must all be bought. Both chains that do this
    file "the second one is a shekel" the same way, and the whole deal is the
    sum of its legs:

        Super-Pharm   group 1  one bottle 28.90   group 2  one bottle 1.00
                      -> two for 29.90, 14.95 each

        Fresh Market  group 1  spend 75.00        (a threshold, not a leg)
                      group 2  one tin, no price  (whatever the branch charges)
                      group 3  one tin, 1.00
                      -> two for shelf + 1.00

    Read the groups apart instead and the shekel looks like the price of the
    product, which is what the app once showed for a bottle of Listerine.

    Threshold groups are skipped before anything else. They can never contain a
    real barcode, so counting them would demand that the tin appear in a group
    it cannot be in - which silently discarded a genuine Fresh Market discount
    at 46 branches.

    A leg with no price of its own is reported rather than guessed at:
    ``unpriced_qty`` says how much of the quantity still needs a shelf price,
    and ``known`` is the total of the legs that did state one. Resolving it
    needs the branch's own price, so build_chain_db.py finishes the sum once
    the price tables exist. Publishing the known part alone would advertise two
    tins for a shekel.

    Only a barcode in *every* remaining group is priced. Missing from one and
    the deal is a cross-product one - "buy a Cerruti perfume, get the 9.5 ml
    free" - which changes neither item's own price and cannot honestly be
    published as a price for either.
    """
    shared = set(legs[0])
    for other in legs[1:]:
        shared &= set(other)

    for barcode in shared:
        rows = [leg[barcode] for leg in legs]
        # A fractional MinQty is a weight (see the note at the top of this
        # file); summing it with a count would be nonsense, so leave those.
        if any(qty is None or qty < 1 for qty, _ in rows):
            continue
        qty = sum(qty for qty, _ in rows)
        known = sum(price for _, price in rows if price is not None)
        unpriced = sum(q for q, price in rows if price is None)
        yield barcode, qty, (known if not unpriced else None), known, unpriced


def _items(promotion):
    """``(barcode, min_qty, price, known, unpriced_qty)`` per item.

    ``price`` is the whole cost when it is knowable from the promotion alone,
    and None when a leg priced itself at "whatever the branch charges". In that
    case ``known`` holds the legs that did state a price and ``unpriced_qty``
    the quantity still waiting on one; an ordinary offer carries known == price
    and unpriced_qty == 0, so callers that do not care can ignore both.
    """
    groups = list(promotion.iter("Group"))
    if len(groups) > 1:
        legs = _legs(groups)
        if len(legs) > 1:
            # A real multi-leg deal. It may yield nothing - a cross-product
            # gift has no barcode in every leg - and that silence is the
            # answer, not a reason to fall through and price the qualifying
            # item as though its own price had changed.
            yield from _across_legs(legs)
            return
        # Every group but one was a spend threshold, so there is no multi-leg
        # deal here; read it as the ordinary promotion it is.
    nested = list(promotion.iter("PromotionItem"))
    if nested:
        for item in nested:
            price = _number(_text(item, "DiscountedPrice"))
            yield (_text(item, "ItemCode"),
                   _number(_text(item, "MinQty")),
                   price, price, 0.0)
        return
    # Flat: the terms sit on the promotion and the items are a bare code list.
    min_qty = _number(_text(promotion, "MinQty"))
    price = _number(_text(promotion, "DiscountedPrice"))
    container = promotion.find("PromotionItems")
    for item in (container if container is not None else []):
        yield (_text(item, "ItemCode"), min_qty, price, price, 0.0)


def find_promo_files(dumps_dir):
    """Every PromoFull dump, whatever the chain named it."""
    found = []
    for root, _dirs, files in os.walk(dumps_dir):
        if os.path.basename(root) in ("status", "outputs"):
            continue
        for name in files:
            if name.lower().startswith("promofull") and name.endswith(".xml"):
                found.append(os.path.join(root, name))
    return sorted(found)


def read_offers(dumps_dir):
    """Yield ``(store_id, offer)`` for every usable promotion item.

    ``offer`` is ``(chain_id, promo_id, barcode, club, coupon, min_qty,
    price, unit_price, description, starts, ends)`` - the same tuple for both
    dialects, so a caller never learns which one a branch used.
    """
    for path in find_promo_files(dumps_dir):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            print(f"[promos] unreadable {os.path.basename(path)}: {exc}")
            continue

        chain_id = "".join(ch for ch in _text(root, "ChainID", "ChainId") if ch.isdigit())
        store_id = str(_text(root, "StoreID", "StoreId")).lstrip("0") or "0"
        promotions = root.find("Promotions")
        if not chain_id or promotions is None:
            continue

        for promotion in promotions:
            promo_id = _text(promotion, "PromotionID", "PromotionId").lstrip("0")
            if not promo_id:
                continue
            club, _raw = club_of(promotion)
            coupon = coupon_of(promotion)
            description = _text(promotion, "PromotionDescription")
            starts = _text(promotion, "PromotionStartDateTime", "PromotionStartDate")[:10]
            ends = _text(promotion, "PromotionEndDateTime", "PromotionEndDate")[:10]

            for barcode, min_qty, price, known, unpriced in _items(promotion):
                barcode = "".join(ch for ch in barcode if ch.isdigit())
                if len(barcode) < 6:
                    continue
                # An offer still waiting on a shelf price has no `price` yet,
                # and must not be judged against MIN_PRICE on the strength of
                # the leg that happens to be known.
                if not unpriced and (price is None or price < MIN_PRICE):
                    continue                      # loyalty blanket, not a price
                if min_qty is None or min_qty <= 0 or min_qty >= MAX_MIN_QTY:
                    continue                      # basket promotion, not a price
                # A fractional MinQty is a weight, not a count: the price is
                # already per kilo and there is nothing to divide, nor any
                # "buy 2" to report. See the note at the top of this file.
                if min_qty < 1:
                    effective_qty, unit_price = 1.0, price
                else:
                    effective_qty = min_qty
                    unit_price = None if price is None else price / min_qty
                yield store_id, Offer(
                    chain_id=chain_id,
                    promo_id=promo_id,
                    barcode=barcode,
                    club=club,
                    coupon=coupon,
                    min_qty=round(effective_qty, 3),
                    price=None if price is None else round(price, 2),
                    unit_price=None if unit_price is None else round(unit_price, 2),
                    description=description,
                    starts=starts,
                    ends=ends,
                    known_price=round(known or 0.0, 2),
                    unpriced_qty=round(unpriced, 3),
                )
