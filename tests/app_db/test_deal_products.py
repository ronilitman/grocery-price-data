"""build_app_db.py's deal_products table (KAN-15): one row per product with a
real discount, representing it by the deal with the lowest unit_price.

Shares the fixture-building style of tests/app_db/test_deals.py, but needs a
barcode on offer at more than one chain to exercise "the cheapest-unit-price
chain wins, and its own discount_pct decides the sort position even when a
different chain's offer on the same barcode is a bigger percentage off".
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402

TODAY = "2026-09-14"

# On offer at two chains: RAMI_LEVY's is the bigger discount (80%) but a
# higher unit_price (10.00); SHUFERSAL's is a smaller discount (10%) but the
# cheaper unit_price (5.00) - unit_price is what picks the representative,
# not discount_pct, so SHUFERSAL's row (and its 10% discount) must win, even
# though RAMI_LEVY's 80% would sort far higher in a naive discount-first scan.
MULTI_CHAIN = "9000000000001"
# On offer at one chain only, with a real discount.
SINGLE_CHAIN = "9000000000002"
# A deal exists but discount_pct <= 0 (multi-buy no cheaper than shelf) -
# must not produce a deal_products row, and must not count toward another
# barcode's chains_on_deal.
ZERO_DISCOUNT_ONLY = "9000000000003"
# Two deals at the SAME chain (e.g. a club-card variant) with equal
# unit_price - tie broken by the lower deal_id, and chains_on_deal counts the
# chain once, not twice.
TIE_SAME_CHAIN = "9000000000004"
NO_BASELINE_NO_DISCOUNT = "9000000000005"


@pytest.fixture
def fixture_db(tmp_path):
    import merge_db

    path = str(tmp_path / "prices.db")
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?,?)", [
        ("RAMI_LEVY", "Rami Levy", None),
        ("SHUFERSAL", "Shufersal", None),
    ])
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "10", None, "Rami Levy A", "3000", None, "St 1", 100, None, None, None, None),
            ("SHUFERSAL", "1", None, "Shufersal A", "5000", None, "St 2", 100, None, None, None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (MULTI_CHAIN, "Coffee", "Acme", "200", 200.0, "g", 0),
            (SINGLE_CHAIN, "Tea", "Acme", "100", 100.0, "g", 0),
            (ZERO_DISCOUNT_ONLY, "Sugar", "Acme", "1", 1.0, "kg", 0),
            (TIE_SAME_CHAIN, "Rice", "Acme", "1", 1.0, "kg", 0),
            (NO_BASELINE_NO_DISCOUNT, "Pasta", "Acme", "500", 500.0, "g", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", MULTI_CHAIN, 50.00, 3),   # -> 80% off at 10.00
            ("SHUFERSAL", MULTI_CHAIN, 5.56, 3),    # -> 10.1% off at 5.00
            ("RAMI_LEVY", SINGLE_CHAIN, 10.00, 3),  # -> 50% off at 5.00
            ("RAMI_LEVY", ZERO_DISCOUNT_ONLY, 4.00, 3),  # deal is not cheaper
            ("RAMI_LEVY", TIE_SAME_CHAIN, 20.00, 3),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RAMI_LEVY", "P1", MULTI_CHAIN, 0, 0, 1, 10.00, 10.00,
             "coffee at rami levy", "2026-09-01", "2026-12-31"),
            (2, "SHUFERSAL", "P2", MULTI_CHAIN, 0, 0, 1, 5.00, 5.00,
             "coffee at shufersal - cheaper unit price, smaller %", "2026-09-01", "2026-12-31"),
            (3, "RAMI_LEVY", "P3", SINGLE_CHAIN, 0, 0, 1, 5.00, 5.00,
             "tea deal", "2026-09-01", "2026-12-31"),
            # Multi-buy that is not actually cheaper than the shelf price
            # (4.00 unit_price vs 4.00 base) -> discount_pct == 0, must be
            # excluded from deal_products entirely.
            (4, "RAMI_LEVY", "P4", ZERO_DISCOUNT_ONLY, 0, 0, 2, 8.00, 4.00,
             "not actually cheaper", "2026-09-01", "2026-12-31"),
            # Two RAMI_LEVY offers on the same barcode, same unit_price
            # (18.00): a plain deal (id 5) and a club-card deal (id 6) at a
            # DIFFERENT branch, so neither dominates the other and both
            # survive the merge - deal_id breaks the tie.
            (5, "RAMI_LEVY", "P5", TIE_SAME_CHAIN, 0, 0, 1, 18.00, 18.00,
             "rice deal A", "2026-09-01", "2026-12-31"),
            (6, "RAMI_LEVY", "P6", TIE_SAME_CHAIN, 1, 0, 1, 18.00, 18.00,
             "rice deal B - club card, different branch", "2026-09-01", "2026-12-31"),
            # No chain_prices baseline at all -> discount_pct is NULL, must
            # be excluded from deal_products.
            (7, "SHUFERSAL", "P7", NO_BASELINE_NO_DISCOUNT, 0, 0, 1, 3.00, 3.00,
             "pasta, no baseline", "2026-09-01", "2026-12-31"),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_stores VALUES (?,?)",
        [
            (1, "10"), (2, "1"), (3, "10"), (4, "10"),
            (5, "10"),
            # A second RAMI_LEVY branch so deal 6 (club) doesn't get pruned
            # as dominated-everywhere by deal 5 at the same branch.
            (6, "11"),
            (7, "1"),
        ],
    )
    # A second RAMI_LEVY branch, for the tie-break fixture above.
    conn.execute(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("RAMI_LEVY", "11", None, "Rami Levy B", "3000", None, "St 3", 100, None, None, None, None),
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


def _row(conn, barcode):
    cols = ("barcode", "deal_id", "chain_id", "category_id", "name",
            "base_price", "unit_price", "price", "min_qty", "club", "coupon",
            "ends", "description", "discount_pct", "chains_on_deal")
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM deal_products WHERE barcode = ?",
        (barcode,)).fetchone()
    return dict(zip(cols, row)) if row else None


def test_lowest_unit_price_wins_even_with_a_smaller_discount_pct(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    row = _row(conn, MULTI_CHAIN)
    conn.close()

    assert row is not None
    # Shufersal's 5.00/unit beats Rami Levy's 10.00/unit, even though Rami
    # Levy's percentage off (80%) is far bigger than Shufersal's (~10%).
    assert row["chain_id"] == "SHUFERSAL"
    assert row["unit_price"] == 5.00
    assert row["discount_pct"] == pytest.approx(10.1, abs=0.05)
    assert row["chains_on_deal"] == 2


def test_single_chain_product_reports_one_chain(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    row = _row(conn, SINGLE_CHAIN)
    conn.close()
    assert row["chain_id"] == "RAMI_LEVY"
    assert row["chains_on_deal"] == 1
    assert row["discount_pct"] == 50.0


def test_non_positive_discount_is_excluded(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    row = _row(conn, ZERO_DISCOUNT_ONLY)
    conn.close()
    assert row is None


def test_null_discount_is_excluded(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    row = _row(conn, NO_BASELINE_NO_DISCOUNT)
    conn.close()
    assert row is None


def test_tie_on_unit_price_breaks_on_lowest_deal_id_and_chain_counted_once(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    row = _row(conn, TIE_SAME_CHAIN)
    deal_ids = {r[0] for r in conn.execute(
        "SELECT deal_id FROM deals WHERE barcode = ?", (TIE_SAME_CHAIN,))}
    conn.close()

    assert row["deal_id"] == min(deal_ids)
    assert row["chains_on_deal"] == 1  # both offers are RAMI_LEVY


def test_row_count_matches_distinct_positive_discount_barcodes(fixture_db, tmp_path):
    conn = _build(fixture_db, tmp_path)
    (expected,) = conn.execute(
        "SELECT COUNT(DISTINCT barcode) FROM deals WHERE discount_pct > 0"
    ).fetchone()
    (actual,) = conn.execute("SELECT COUNT(*) FROM deal_products").fetchone()
    conn.close()
    assert actual == expected == 3  # MULTI_CHAIN, SINGLE_CHAIN, TIE_SAME_CHAIN
