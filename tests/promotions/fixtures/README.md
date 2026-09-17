# Promotion fixtures

One directory per scenario, each holding a real `PromoFull` dump carved down to
the promotion under test. The header (`ChainID`, `SubChainID`, `StoreID`) is
kept intact and the filename keeps the chain's own naming convention, so
`read_offers()` walks these exactly as it walks a production dumps directory —
`find_promo_files()` matches on the `PromoFull` prefix, and the branch is read
out of the XML rather than the name.

**Nothing here is hand-written.** Every file was cut from a dump downloaded from
the chain's own portal. The bugs these guard against were all of the form "the
file did not mean what we assumed it meant", and an invented fixture would
encode the assumption instead of the file.

| directory | what it holds | why it is here |
|---|---|---|
| `super_pharm_second_at_one_shekel` | two groups, both priced: a bottle at 28.90 and a second at 1.00 | the shekel leg escaped on its own and the app advertised a ₪1 bottle of Listerine |
| `fresh_market_second_at_one_shekel` | three groups: a ₪75 spend threshold, a tin with **no** price, a tin at 1.00 | the threshold made "the barcode must be in every group" impossible to satisfy, losing a real discount at 46 branches |
| `super_pharm_gift_with_purchase` | two groups holding different products — buy a Cerruti perfume, get a 9.5 ml miniature | neither item's own price changes, so nothing may be published; the qualifying perfume's 221.00 is simply what it costs |
| `super_pharm_single_group_discount` | one group, one price, coupon flag set | the ordinary shape most promotions have — a regression guard for everything above |
| `victory_buy_two_get_one_free` | Victory 2+1 tehina, promo 260963: a buy leg at 14.90, a `DiscountRate=100` gift leg at 0 | KAN-26: the buy leg's own price is right here, but must still be resolved through the branch's real shelf price, not trusted directly |
| `victory_gift_not_stocked` | Victory 2+1 Palmolive, promo 261418, 15 interchangeable barcodes per group; branch 097 stocks neither checked barcode, so both buy legs are junk (0.02) | the actual KAN-26 bug: the app advertised these at ₪0.01 |
| `victory_buy_one_get_one_free` | Victory 1+1 peanut butter, promo 260950 | the (N=1, M=1) case of the same shape |
| `victory_second_at_half_price` | Victory "second at 50%", promo 261407: a buy leg per product, a `DiscountRate=50` reward leg with its own (untrusted) half price | the third shape KAN-26 measured: nothing is free, so the arithmetic generalises rather than special-cases the gift |
| `victory_single_group_discount` | one group, one item, `DiscountRate=-49400` | a real garbage value pulled from this dump — proves the leg-pairing logic never reads `DiscountRate` outside a genuine buy/reward pair |

## Refreshing one

The chains keep only a few days of files, so a fixture cannot be re-downloaded
later. If a new shape needs covering, scrape the chain, find the promotion, and
carve it with its header rather than editing an existing file by hand:

```python
root = ET.Element("Root")
for tag, value in [("ChainID", chain), ("SubChainID", sub), ("StoreID", store)]:
    ET.SubElement(root, tag).text = value
promotions = ET.SubElement(root, "Promotions")
promotions.append(the_promotion_element)      # untouched, as published
```

Record in the table above what the file demonstrates and which real failure it
stands for. A fixture whose purpose nobody remembers is one nobody dares change.
