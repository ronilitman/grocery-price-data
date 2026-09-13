"""build_app_db.py's deals/store_bits tables against a small fixture prices.db.

The fixture stands in for a real prices.db, built with merge_db.SCHEMA, with
enough offers to exercise every rule KAN-7 specifies:

  * a MinQty below 1 is a weight, not a pack size, and gets fixed to
    ``min_qty=1, unit_price=price`` (the Rami Levy tomato bug);
  * an offer whose ``ends`` is before the build date is absent entirely;
  * an offer dominated everywhere it runs, by something cheaper on the same
    (barcode, club, coupon), is absent;
  * ``discount_pct`` matches a hand computation, and is NULL with no baseline
    chain_prices row;
  * a deal's ``branches`` bitmap, unpacked through ``store_bits``, names
    exactly the branches that honoured the offer.

This exercises scripts/offers.py (shared with build_catalog.py's promo/ JSON)
and scripts/bitmap.py together with build_app_db.build().
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bitmap  # noqa: E402
import build_app_db  # noqa: E402
import merge_db  # noqa: E402

TODAY = "2026-09-13"

# Barcode with a chain_prices baseline (for discount_pct), one without.
WITH_BASELINE = "1111111111111"      # Ketchup: domination + expiry cases
WEIGHT_BUG = "2222222222222"         # MinQty 0.01 tomato-style bug
NO_BASELINE = "3333333333333"        # Shufersal-only, no chain_prices row
# Regression fixtures for the review fix: a chain's bit space must be built
# from stores UNION promo_stores, not stores alone - CITY_MARKET_SHOPS and
# YELLOW are real chains whose promo branches are absent or partly absent
# from `stores` (see the KAN-7 review comment).
ZERO_STORES_ONLY = "4444444444444"   # chain has NO rows in `stores` at all
PARTIAL_STORES_ONLY = "5555555555555"  # chain's `stores` misses one branch


@pytest.fixture
def fixture_db(tmp_path):
    path = str(tmp_path / "prices.db")
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?)", [
        ("RAMI_LEVY", "Rami Levy"),
        ("SHUFERSAL", "Shufersal"),
        ("ZERO_STORES", "Zero Stores Chain"),
        ("PARTIAL_STORES", "Partial Stores Chain"),
    ])
    # RAMI_LEVY: three branches, sorted "10" < "20" < "30" -> bits 0, 1, 2.
    # SHUFERSAL: one branch -> bit 0.
    # ZERO_STORES: deliberately has no rows here at all (like YELLOW).
    # PARTIAL_STORES: `stores` only knows about "p1"; "p2" is promo-only
    # (like the 9-of-425 Shufersal gap and the קשת טעמים gap).
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "10", None, "Rami Levy A", "3000", "St 1", 100),
            ("RAMI_LEVY", "20", None, "Rami Levy B", "3000", "St 2", 100),
            ("RAMI_LEVY", "30", None, "Rami Levy C", "3000", "St 3", 100),
            ("SHUFERSAL", "1", None, "Shufersal Deal", "5000", "St 4", 100),
            ("PARTIAL_STORES", "p1", None, "Partial A", "1000", "St 5", 100),
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (WITH_BASELINE, "Ketchup", "Acme", "750", 750.0, "g", 0),
            (WEIGHT_BUG, "Tomatoes", "Acme", "1", 1.0, "kg", 1),
            (NO_BASELINE, "Crackers", "Acme", "200", 200.0, "g", 0),
            (ZERO_STORES_ONLY, "Yellow Snacks", "Acme", "100", 100.0, "g", 0),
            (PARTIAL_STORES_ONLY, "Partial Snacks", "Acme", "100", 100.0, "g", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", WITH_BASELINE, 10.00, 3),
            ("RAMI_LEVY", WEIGHT_BUG, 3.50, 3),
            # No chain_prices row for (SHUFERSAL, NO_BASELINE) on purpose.
        ],
    )

    # promo_offers: offer_id, chain_id, promo_id, barcode, club, coupon,
    # min_qty, price, unit_price, description, starts, ends
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # 1: the survivor - 8.00/unit at branches 10 and 20.
            (1, "RAMI_LEVY", "P1", WITH_BASELINE, 0, 0, 1, 8.00, 8.00,
             "cheap ketchup", "2026-09-01", "2026-12-31"),
            # 2: dominated - 9.00/unit, only at branch 10, which offer 1
            # already covers at a cheaper price.
            (2, "RAMI_LEVY", "P2", WITH_BASELINE, 0, 0, 1, 9.00, 9.00,
             "worse ketchup deal", "2026-09-01", "2026-12-31"),
            # 3: expired - ends before TODAY, must not appear at all.
            (3, "RAMI_LEVY", "P3", WITH_BASELINE, 0, 0, 1, 7.00, 7.00,
             "expired ketchup deal", "2026-01-01", "2026-02-01"),
            # 4: the MinQty-is-a-weight bug, at branch 10.
            (4, "RAMI_LEVY", "P4", WEIGHT_BUG, 0, 0, 0.01, 2.90, 290.00,
             "loose tomatoes", "2026-09-01", "2026-12-31"),
            # 5: no chain_prices baseline for this chain+barcode.
            (5, "SHUFERSAL", "P5", NO_BASELINE, 0, 0, 1, 5.00, 5.00,
             "crackers deal", "2026-09-01", "2026-12-31"),
            # 6: ZERO_STORES chain - promo_stores names "z1"/"z2" but `stores`
            # has no rows for this chain at all.
            (6, "ZERO_STORES", "P6", ZERO_STORES_ONLY, 0, 0, 1, 4.00, 4.00,
             "yellow snacks deal", "2026-09-01", "2026-12-31"),
            # 7: PARTIAL_STORES chain - runs at "p1" (known to `stores`) and
            # "p2" (promo-only, missing from `stores`).
            (7, "PARTIAL_STORES", "P7", PARTIAL_STORES_ONLY, 0, 0, 1, 6.00, 6.00,
             "partial snacks deal", "2026-09-01", "2026-12-31"),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_stores VALUES (?,?)",
        [
            (1, "10"), (1, "20"),
            (2, "10"),
            (3, "30"),
            (4, "10"),
            (5, "1"),
            (6, "z1"), (6, "z2"),
            (7, "p1"), (7, "p2"),
        ],
    )
    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", f"{TODAY}T02:00:00+00:00"),
        ("chain_as_of", json.dumps({})),
        ("sources", json.dumps(["rami_levy.db", "shufersal.db"])),
    ])
    conn.commit()
    conn.close()
    return path


def _build(fixture_db, tmp_path):
    out_path = str(tmp_path / "app.db")
    build_app_db.build(fixture_db, out_path)
    return sqlite3.connect(out_path)


def _deal_for(conn, barcode):
    rows = conn.execute(
        "SELECT deal_id, chain_id, barcode, unit_price, price, min_qty, "
        "discount_pct, branches FROM deals WHERE barcode = ?", (barcode,)
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one deal for {barcode}, got {rows}"
    return rows[0]


def _branch_set(conn, chain_id, blob):
    """Unpack a bitmap via store_bits: bit -> store_id for that chain."""
    bit_to_store = dict(conn.execute(
        "SELECT bit, store_id FROM store_bits WHERE chain_id = ?", (chain_id,)))
    return {bit_to_store[i] for i in range(len(blob) * 8)
            if bitmap.has_branch(blob, i) and i in bit_to_store}


def test_expired_offer_is_absent(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    descriptions = {row[0] for row in conn.execute(
        "SELECT description FROM deals WHERE barcode = ?", (WITH_BASELINE,))}
    conn.close()
    assert "expired ketchup deal" not in descriptions


def test_dominated_offer_is_absent(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    descriptions = {row[0] for row in conn.execute(
        "SELECT description FROM deals WHERE barcode = ?", (WITH_BASELINE,))}
    conn.close()
    # Only the survivor (offer 1) remains; offer 2 was dominated at branch 10.
    assert descriptions == {"cheap ketchup"}


def test_the_survivor_has_both_branches_and_correct_discount(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    deal_id, chain_id, barcode, unit_price, price, min_qty, discount_pct, branches = \
        _deal_for(conn, WITH_BASELINE)
    branch_set = _branch_set(conn, chain_id, branches)
    conn.close()

    assert unit_price == 8.00
    assert price == 8.00
    assert min_qty == 1.0
    assert branch_set == {"10", "20"}
    # base_price 10.00, unit_price 8.00 -> round((1 - 0.8) * 100, 1) == 20.0
    assert discount_pct == 20.0


def test_min_qty_below_one_is_fixed_to_a_pack_of_one(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    _, _, _, unit_price, price, min_qty, discount_pct, _ = _deal_for(conn, WEIGHT_BUG)
    conn.close()

    assert min_qty == 1.0
    assert unit_price == price == 2.90
    # base_price 3.50, unit_price 2.90 -> round((1 - 2.9/3.5) * 100, 1) == 17.1
    assert discount_pct == 17.1


def test_discount_pct_is_null_with_no_baseline(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    _, _, _, _, _, _, discount_pct, _ = _deal_for(conn, NO_BASELINE)
    conn.close()

    assert discount_pct is None


def test_bitmap_roundtrip_matches_the_offers_branch_set(fixture_db, tmp_path):
    """Every deal's bitmap, unpacked via store_bits, names exactly its
    original promo_stores branch set - checked against the source fixture,
    not against the merge logic under test."""
    conn = _build(fixture_db, tmp_path)

    src = sqlite3.connect(fixture_db)
    expected = {
        WITH_BASELINE: {"10", "20"},   # offer 1's branches (offer 2 pruned)
        WEIGHT_BUG: {"10"},
        NO_BASELINE: {"1"},
    }
    src.close()

    for barcode, want in expected.items():
        deal_id, chain_id, _, _, _, _, _, branches = _deal_for(conn, barcode)
        got = _branch_set(conn, chain_id, branches)
        assert got == want, f"{barcode}: bitmap decoded to {got}, expected {want}"
    conn.close()


def test_store_bits_cover_every_branch_of_each_chain(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    rows = conn.execute(
        "SELECT chain_id, store_id, bit FROM store_bits ORDER BY chain_id, bit"
    ).fetchall()
    conn.close()

    assert rows == [
        ("PARTIAL_STORES", "p1", 0),
        ("PARTIAL_STORES", "p2", 1),
        ("RAMI_LEVY", "10", 0),
        ("RAMI_LEVY", "20", 1),
        ("RAMI_LEVY", "30", 2),
        ("SHUFERSAL", "1", 0),
        ("ZERO_STORES", "z1", 0),
        ("ZERO_STORES", "z2", 1),
    ]


def test_deals_row_count_matches_survivors(fixture_db, tmp_path):
    """7 offers in, 1 expired, 1 dominated -> 5 deals rows."""
    conn = _build(fixture_db, tmp_path)
    count = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    conn.close()
    assert count == 5


def test_store_bits_cover_a_chain_with_zero_stores_rows(fixture_db, tmp_path):
    """ZERO_STORES has no `stores` rows at all - like the real YELLOW chain -
    yet its promo-only branches "z1"/"z2" must still get bits, and the deal's
    bitmap must round-trip to exactly that branch set."""
    conn = _build(fixture_db, tmp_path)

    bits = conn.execute(
        "SELECT store_id, bit FROM store_bits WHERE chain_id = 'ZERO_STORES' "
        "ORDER BY bit").fetchall()
    assert bits == [("z1", 0), ("z2", 1)]

    deal_id, chain_id, _, _, _, _, _, branches = _deal_for(conn, ZERO_STORES_ONLY)
    branch_set = _branch_set(conn, chain_id, branches)
    conn.close()
    assert branch_set == {"z1", "z2"}


def test_store_bits_cover_promo_only_branches_missing_from_stores(fixture_db, tmp_path):
    """PARTIAL_STORES' `stores` table only knows "p1"; "p2" is promo-only -
    like the 9-of-425 Shufersal gap and the קשת טעמים gap on the real data.
    Both must get bits, and the deal's bitmap must round-trip exactly."""
    conn = _build(fixture_db, tmp_path)

    bits = conn.execute(
        "SELECT store_id, bit FROM store_bits WHERE chain_id = 'PARTIAL_STORES' "
        "ORDER BY bit").fetchall()
    assert bits == [("p1", 0), ("p2", 1)]

    deal_id, chain_id, _, _, _, _, _, branches = _deal_for(conn, PARTIAL_STORES_ONLY)
    branch_set = _branch_set(conn, chain_id, branches)
    conn.close()
    assert branch_set == {"p1", "p2"}


def test_build_deals_raises_when_store_bits_is_missing_a_branch(fixture_db, tmp_path):
    """The invariant is enforced in code: if a kept offer's branch set isn't
    fully representable in store_bits, the build must fail loudly rather than
    silently drop the branch from the bitmap (the review-fix bug: 61,089 real
    deals ending up with an all-zero bitmap instead of a raised error)."""
    conn = sqlite3.connect(str(tmp_path / "direct.db"))
    conn.executescript(build_app_db.SCHEMA)
    conn.execute("ATTACH DATABASE ? AS src", (build_app_db.ro_uri(fixture_db),))

    bit_of, _ = build_app_db.build_store_bits(conn)
    conn.commit()
    # Tamper: drop RAMI_LEVY's bit for branch "20", which offer 1 (the
    # WITH_BASELINE survivor) actually runs at.
    del bit_of["RAMI_LEVY"]["20"]

    with pytest.raises(ValueError, match="RAMI_LEVY"):
        build_app_db.build_deals(conn, bit_of, {})
    conn.close()
