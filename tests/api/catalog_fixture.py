"""A small prices.db fixture that exercises every KAN-13 endpoint rule, plus
the helpers to run build_catalog.py and build_app_db.py against it and read
their output back for the equivalence tests (tests/api/test_equivalence.py).

Coverage, one barcode per DoD line item:

  DAIRY           - a plain packaged product: baseline price only.
  BRANCH_EXC      - a branch that charges something other than the baseline
                    (price_exceptions).
  CLUB_OFFER      - a promo_offers row with club=1.
  COUPON_OFFER    - a promo_offers row with coupon=1.
  EXPIRED_TRAP    - the "everywhere" trap: a live offer at branches 10,20 of
                    RAMI_LEVY and an EXPIRED offer at branch 30 - the only
                    offer that chain ever ran there. merge_chain's
                    `everywhere` therefore includes branch 30 (unioned
                    before the expiry filter); `deals` does not, since it
                    only carries kept, unexpired offers. RAMI_LEVY also has
                    a fourth store (40) that never runs ANY promotion at
                    all, so store_bits' full per-chain universe
                    ({10,20,30,40}) also disagrees with the true
                    `everywhere` ({10,20,30}) - the fixture pins down both
                    wrong approximations at once (see
                    test_equivalence.py::test_expired_offer_still_counts_...).
  WEIGHED_CODE    - stored as "000123"; candidate resolution finds it from
                    scanning "729000000123" (the spec's own example).
  UPC_A_CODE      - stored as a 12-digit code; candidate resolution finds it
                    from scanning the 13-digit form with a leading zero.
"""
import os
import sqlite3
import sys

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
_SCRIPTS_DIR = os.path.join(_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

TODAY = "2026-09-14"

DAIRY = "1000000000001"
BRANCH_EXC = "1000000000002"
CLUB_OFFER = "1000000000003"
COUPON_OFFER = "1000000000004"
EXPIRED_TRAP = "1000000000005"
WEIGHED_CODE = "000123"          # scanned as 729000000123
UPC_A_CODE = "123456789012"      # scanned as 0123456789012


def _module(name):
    """Import a scripts/ module the same way build_catalog.py/build_app_db.py
    resolve their own bare sibling imports: scripts/ itself is on sys.path
    (inserted above) - unlike api/catalog.py, which imports these modules as
    `scripts.X` and never needs to run the two build scripts themselves."""
    import importlib
    return importlib.import_module(name)


def build_prices_db(path):
    """Write the fixture prices.db (merge_db.SCHEMA) to `path`."""
    merge_db = _module("merge_db")
    conn = sqlite3.connect(path)
    conn.executescript(merge_db.SCHEMA)

    conn.executemany("INSERT INTO chains VALUES (?,?)", [
        ("RAMI_LEVY", "Rami Levy"),
        ("SHUFERSAL", "Shufersal"),
    ])

    # RAMI_LEVY: branches 10,20,30 sorted -> bits 0,1,2; "40" is a real store
    # that never appears in any promo_offers/promo_stores row at all - the
    # second half of the "everywhere" trap (see module docstring).
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", "10", None, "Rami Levy A", "3000", "St 1", 100),
            ("RAMI_LEVY", "20", None, "Rami Levy B", "3000", "St 2", 100),
            ("RAMI_LEVY", "30", None, "Rami Levy C", "3000", "St 3", 100),
            ("RAMI_LEVY", "40", None, "Rami Levy D", "3000", "St 4", 100),
            ("SHUFERSAL", "1", None, "Shufersal Deal", "5000", "St 5", 100),
            ("SHUFERSAL", "2", None, "Shufersal Other", "5000", "St 6", 100),
        ],
    )

    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (DAIRY, "גבינה לבנה 9%", "Acme", "250", 250.0, "g", 0),
            (BRANCH_EXC, "לחם אחיד", "Acme", "750", 750.0, "g", 0),
            (CLUB_OFFER, "יין אדום", "Acme", "750", 750.0, "מ\"ל", 0),
            (COUPON_OFFER, "שוקולד חלב", "Acme", "100", 100.0, "g", 0),
            (EXPIRED_TRAP, "פודינג וניל", "Acme", "80", 80.0, "g", 0),
            (WEIGHED_CODE, "בננה במשקל", "Acme", "kg", 1.0, "kg", 1),
            (UPC_A_CODE, "קפה נמס", "Acme", "200", 200.0, "g", 0),
        ],
    )

    conn.executemany(
        "INSERT INTO chain_prices VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", DAIRY, 6.90, 4),
            ("RAMI_LEVY", BRANCH_EXC, 8.50, 4),
            ("RAMI_LEVY", CLUB_OFFER, 39.90, 4),
            ("RAMI_LEVY", COUPON_OFFER, 12.90, 4),
            ("RAMI_LEVY", EXPIRED_TRAP, 5.90, 4),
            ("RAMI_LEVY", WEIGHED_CODE, 5.90, 4),
            ("RAMI_LEVY", UPC_A_CODE, 22.90, 4),
        ],
    )

    conn.executemany(
        "INSERT INTO price_exceptions VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", "20", BRANCH_EXC, 7.50),
        ],
    )

    conn.executemany(
        "INSERT INTO chain_products VALUES (?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", WEIGHED_CODE, "בננה במשקל", "1", "kg", 1),
        ],
    )

    # promo_offers: offer_id, chain_id, promo_id, barcode, club, coupon,
    # min_qty, price, unit_price, description, starts, ends
    conn.executemany(
        "INSERT INTO promo_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "RAMI_LEVY", "P1", CLUB_OFFER, 1, 0, 1, 29.90, 29.90,
             "club price on red wine", "2026-09-01", "2026-12-31"),
            (2, "RAMI_LEVY", "P2", COUPON_OFFER, 0, 1, 1, 9.90, 9.90,
             "app coupon on chocolate", "2026-09-01", "2026-12-31"),
            # The live survivor: 3.90/unit at branches 10 and 20.
            (3, "RAMI_LEVY", "P3", EXPIRED_TRAP, 0, 0, 3, 11.70, 3.90,
             "3 for 11.70 vanilla pudding", "2026-09-01", "2027-12-31"),
            # The EXPIRED offer at branch 30 - the only promotion that
            # branch ever ran for this chain. Ends before TODAY, so it must
            # be absent from `deals`, but its branch still belongs to
            # merge_chain's `everywhere` (unioned before the expiry check).
            (4, "RAMI_LEVY", "P4", EXPIRED_TRAP, 0, 0, 1, 4.90, 4.90,
             "expired single-branch deal", "2026-01-01", "2026-02-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO promo_stores VALUES (?,?)",
        [
            (1, "10"), (1, "20"), (1, "30"),   # club offer: every branch
            (2, "10"),                          # coupon offer: one branch
            (3, "10"), (3, "20"),                # live pudding offer
            (4, "30"),                           # expired-only branch
        ],
    )

    conn.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [
            ("built_at", f"{TODAY}T02:00:00Z"),
            ("chain_as_of", '{"SHUFERSAL": "2026-09-12"}'),
        ],
    )
    conn.commit()
    conn.close()


def build_app_db_from_fixture(prices_db_path, out_path, tmp_path):
    """Run build_app_db.build() against the fixture."""
    build_app_db = _module("build_app_db")
    counts, _size, _elapsed, _merged_total = build_app_db.build(
        prices_db_path, out_path)
    return counts


def run_build_catalog(prices_db_path, out_dir):
    build_catalog = _module("build_catalog")
    conn = sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True)
    import os as _os
    if _os.path.isdir(out_dir):
        import shutil
        shutil.rmtree(out_dir)
    _os.makedirs(out_dir, exist_ok=True)

    entries = {}
    for barcode, name, unit_qty, is_weighted in conn.execute(
            "SELECT barcode, name, unit_qty, is_weighted FROM products"):
        entry = {"n": name, "p": {}}
        if is_weighted:
            entry["w"] = 1
            unit = (unit_qty or "").strip()
            if unit and unit != build_catalog.UNKNOWN_UNIT:
                entry["u"] = unit
        entries[barcode] = entry
    for barcode, chain_id, price, store_count in conn.execute(
            "SELECT barcode, chain_id, price, store_count FROM chain_prices"):
        entry = entries.get(barcode)
        if entry is not None:
            entry["p"][chain_id] = [price, store_count or 0]
    chain_names = dict(conn.execute("SELECT chain_id, name FROM chains"))
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    built_at = meta.get("built_at", "")
    chain_as_of = _json_loads(meta.get("chain_as_of") or "{}")

    build_catalog.write_detail(conn, out_dir)
    build_catalog.write_promos(conn, out_dir, (built_at or "")[:10] or "0000-00-00")
    conn.close()

    shards = build_catalog.split(list(entries), 0)
    for prefix, barcodes in shards.items():
        payload = {barcode: entries[barcode] for barcode in barcodes}
        path = os.path.join(out_dir, f"{prefix}.json")
        with open(path, "w", encoding="utf-8") as handle:
            _json_dump(payload, handle)

    import json as _json
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        _json.dump({
            "levels": list(build_catalog.LEVELS), "shards": sorted(shards),
            "chains": chain_names, "built_at": built_at,
            "chain_as_of": chain_as_of, "products": len(entries),
        }, handle, ensure_ascii=False)

    return entries


def _json_loads(s):
    import json
    return json.loads(s)


def _json_dump(obj, handle):
    import json
    json.dump(obj, handle, ensure_ascii=False, separators=(",", ":"))


def load_catalog_output(out_dir):
    """Read back everything run_build_catalog() wrote, merging every shard
    file in each directory (the fixture is small enough that brute-forcing
    "read every *.json file, merge, look up by key" is simpler and just as
    correct as replicating the adaptive sharding/depth logic)."""
    import glob
    import json

    def merge_dir(path):
        merged = {}
        for fp in glob.glob(os.path.join(path, "*.json")):
            if os.path.basename(fp) == "index.json":
                continue
            with open(fp, encoding="utf-8") as handle:
                merged.update(json.load(handle))
        return merged

    with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as handle:
        index = json.load(handle)
    barcodes = merge_dir(out_dir)
    detail = merge_dir(os.path.join(out_dir, "detail"))
    promo = merge_dir(os.path.join(out_dir, "promo"))
    return index, barcodes, detail, promo
