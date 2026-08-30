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

The unit price is computed here as ``DiscountedPrice / MinQty`` and never taken
from ``DiscountedPricePerMida``: that field is per *measure* unit, so Stop
Market publishes 8.80 for a 2-for-22 deal on a 125 g box (22 / 2.5 hundred
grams). CHP does the same division, which is how their page shows 11.00 for
that offer.
"""

import os
import xml.etree.ElementTree as ET

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


def _items(promotion):
    """(barcode, min_qty, price) per item, from whichever dialect this is."""
    nested = list(promotion.iter("PromotionItem"))
    if nested:
        for item in nested:
            yield (_text(item, "ItemCode"),
                   _number(_text(item, "MinQty")),
                   _number(_text(item, "DiscountedPrice")))
        return
    # Flat: the terms sit on the promotion and the items are a bare code list.
    min_qty = _number(_text(promotion, "MinQty"))
    price = _number(_text(promotion, "DiscountedPrice"))
    container = promotion.find("PromotionItems")
    for item in (container if container is not None else []):
        yield (_text(item, "ItemCode"), min_qty, price)


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

            for barcode, min_qty, price in _items(promotion):
                barcode = "".join(ch for ch in barcode if ch.isdigit())
                if len(barcode) < 6:
                    continue
                if price is None or price < MIN_PRICE:
                    continue                      # loyalty blanket, not a price
                if min_qty is None or min_qty <= 0 or min_qty >= MAX_MIN_QTY:
                    continue                      # basket promotion, not a price
                yield store_id, (
                    chain_id,
                    promo_id,
                    barcode,
                    club,
                    coupon,
                    round(min_qty, 3),
                    round(price, 2),
                    round(price / min_qty, 2),
                    description,
                    starts,
                    ends,
                )
