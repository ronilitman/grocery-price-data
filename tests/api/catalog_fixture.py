"""A small prices.db fixture that exercises every KAN-13 endpoint rule, plus
the helpers to run build_catalog.py and build_app_db.py against it and read
their output back for the equivalence tests (tests/api/test_equivalence.py).

Deliberately NOT under tests/app_db/ - that directory's conftest.py points
build_app_db at empty (header-only) produce TSVs on purpose, because its
fixtures have no loose produce in them at all. This fixture is built to
match a small, self-contained produce_units.tsv/produce_generic_map.tsv
(write_produce_tsvs below) instead, so the KAN-9 precedence rule
(mapped-key members come from produce_units) has something real to exercise
without touching the real 750/217-row checked-in files.

Coverage, one barcode/key per DoD line item:

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
  TOMATO_A/B/C    - loose produce: TOMATO_A (Rami Levy) and TOMATO_B
                    (Shufersal) both name themselves "עגבניה" and land in
                    the same algorithmic generics.py group; TOMATO_C is a
                    THIRD Shufersal barcode that produce_units.tsv also
                    calls a tomato but which generics.py groups
                    differently (different price band) - so the mapped key
                    "עגבניה" is expected to gain TOMATO_C and drop TOMATO_B
                    once KAN-9 precedence applies, proving the "members
                    differ, on purpose" rule instead of skipping it.
  CUCUMBER_A/B    - loose produce with NO produce_generic_map row at all -
                    resolves exactly as generics.json today (the unmapped
                    path).
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
TOMATO_A = "2000000000001"       # Rami Levy, algorithmic + produce_units
TOMATO_B = "2000000000002"       # Shufersal, algorithmic member only
TOMATO_C = "2000000000003"       # Shufersal, produce_units member only
CUCUMBER_A = "2000000000011"
CUCUMBER_B = "2000000000012"

# KAN-22: PEPPER_SHARED is the SAME barcode at the SAME chain (RAMI_LEVY) in
# BOTH the pepper-hot and pepper-red produce_units rows - the real-world
# collision (chain 7290700100008 / barcode 7290000012872 under both
# pepper-hot and pepper-red). Each slug also has its own distinct SHUFERSAL
# barcode, so the fix must keep the shared RAMI_LEVY price on both slugs
# while leaving each slug's independent SHUFERSAL member untouched.
PEPPER_SHARED = "2000000000021"    # RAMI_LEVY, named by BOTH pepper slugs
PEPPER_HOT_SHUF = "2000000000022"  # SHUFERSAL, pepper-hot only
PEPPER_RED_SHUF = "2000000000023"  # SHUFERSAL, pepper-red only

TOMATO_KEY = "עגבניה"
CUCUMBER_KEY = "מלפפון"
# generics.py's Namer.key() sorts a name's identifying words, so the actual
# generic key is NOT the literal chain_products name - "פלפל חריף" keys as
# "חריף פלפל" (protected word first, alphabetically before "פלפל").
PEPPER_HOT_KEY = "חריף פלפל"
PEPPER_RED_KEY = "אדום פלפל"
TOMATO_SLUG = "tomato"
PEPPER_HOT_SLUG = "pepper-hot"
PEPPER_RED_SLUG = "pepper-red"
CUCUMBER_SLUG_UNMAPPED = None  # cucumber is deliberately left unmapped


def _module(name):
    """Import a scripts/ module the same way tests/app_db/conftest.py does:
    scripts/ itself is on sys.path (inserted above), so build_catalog.py and
    build_app_db.py's own bare sibling imports (`import generics as
    generics_mod`, etc.) resolve - unlike api/catalog.py, which imports
    these modules as `scripts.X` and never needs to run the two build
    scripts themselves."""
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
            (TOMATO_A, "עגבניה", "Acme", "1", 1.0, "kg", 1),
            (TOMATO_B, "עגבניה", "Acme", "1", 1.0, "kg", 1),
            (TOMATO_C, "עגבניה", "Acme", "1", 1.0, "kg", 1),
            (CUCUMBER_A, "מלפפון", "Acme", "1", 1.0, "kg", 1),
            (CUCUMBER_B, "מלפפון", "Acme", "1", 1.0, "kg", 1),
            (PEPPER_SHARED, "פלפל", "Acme", "1", 1.0, "kg", 1),
            (PEPPER_HOT_SHUF, "פלפל חריף", "Acme", "1", 1.0, "kg", 1),
            (PEPPER_RED_SHUF, "פלפל אדום", "Acme", "1", 1.0, "kg", 1),
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
            ("RAMI_LEVY", TOMATO_A, 6.90, 4),
            ("SHUFERSAL", TOMATO_B, 7.90, 2),
            ("SHUFERSAL", TOMATO_C, 14.90, 2),
            ("SHUFERSAL", CUCUMBER_A, 5.90, 2),
            ("RAMI_LEVY", CUCUMBER_B, 4.90, 4),
            ("RAMI_LEVY", PEPPER_SHARED, 9.90, 3),
            ("SHUFERSAL", PEPPER_HOT_SHUF, 11.90, 2),
            ("SHUFERSAL", PEPPER_RED_SHUF, 12.90, 2),
        ],
    )

    conn.executemany(
        "INSERT INTO price_exceptions VALUES (?,?,?,?)",
        [
            ("RAMI_LEVY", "20", BRANCH_EXC, 7.50),
        ],
    )

    # chain_products: only weighed rows matter to generics_mod.from_db, but
    # every (chain,barcode) pair build_deals reads a name from is harmless
    # to include too.
    conn.executemany(
        "INSERT INTO chain_products VALUES (?,?,?,?,?,?)",
        [
            ("RAMI_LEVY", WEIGHED_CODE, "בננה במשקל", "1", "kg", 1),
            ("RAMI_LEVY", TOMATO_A, "עגבניה", "1", "kg", 1),
            ("SHUFERSAL", TOMATO_B, "עגבניה", "1", "kg", 1),
            ("SHUFERSAL", TOMATO_C, "עגבניה", "1", "kg", 1),
            ("RAMI_LEVY", CUCUMBER_B, "מלפפון", "1", "kg", 1),
            ("SHUFERSAL", CUCUMBER_A, "מלפפון", "1", "kg", 1),
            ("RAMI_LEVY", PEPPER_SHARED, "פלפל", "1", "kg", 1),
            ("SHUFERSAL", PEPPER_HOT_SHUF, "פלפל חריף", "1", "kg", 1),
            ("SHUFERSAL", PEPPER_RED_SHUF, "פלפל אדום", "1", "kg", 1),
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


def write_produce_tsvs(dir_path):
    """A minimal produce_units.tsv/produce_generic_map.tsv pair, scoped to
    this fixture's own tomato/cucumber rows - NOT the real 750/217-row
    checked-in files, which a fixture this small can't satisfy (see
    tests/app_db/conftest.py's docstring for why: every mapped slug's
    primary would go stale at once against a tiny catalogue).

    Returns (produce_units_path, produce_generic_map_path).
    """
    units_path = os.path.join(dir_path, "produce_units.tsv")
    map_path = os.path.join(dir_path, "produce_generic_map.tsv")
    header = ("slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
              "chain_product_name\tprice\tnote\n")
    with open(units_path, "w", encoding="utf-8") as handle:
        handle.write(header)
        # Rami Levy's produce_units row matches the algorithmic member
        # (TOMATO_A) exactly. Shufersal's names TOMATO_C - a DIFFERENT
        # barcode than the one the algorithmic grouping picked for
        # Shufersal (TOMATO_B) - so the mapped key is expected to swap
        # members for that chain, on purpose (module docstring).
        handle.write(f"{TOMATO_SLUG}\tעגבניה\tvegetable\tRAMI_LEVY\tRami Levy\t"
                     f"{TOMATO_A}\tעגבניה\t6.9\t\n")
        handle.write(f"{TOMATO_SLUG}\tעגבניה\tvegetable\tSHUFERSAL\tShufersal\t"
                     f"{TOMATO_C}\tעגבניה\t14.9\t\n")
        # KAN-22: pepper-hot and pepper-red both name PEPPER_SHARED for
        # RAMI_LEVY - the same (chain_id, barcode) pair claimed by two
        # different slugs, which is exactly the real collision (chain
        # 7290700100008 / barcode 7290000012872 under both pepper-hot and
        # pepper-red). Each slug also has its own distinct SHUFERSAL member
        # so a batch of both keys must show BOTH the shared RAMI_LEVY price
        # and each slug's own independent SHUFERSAL price.
        handle.write(f"{PEPPER_HOT_SLUG}\tפלפל חריף\tvegetable\tRAMI_LEVY\tRami Levy\t"
                     f"{PEPPER_SHARED}\tפלפל\t9.9\t\n")
        handle.write(f"{PEPPER_HOT_SLUG}\tפלפל חריף\tvegetable\tSHUFERSAL\tShufersal\t"
                     f"{PEPPER_HOT_SHUF}\tפלפל חריף\t11.9\t\n")
        handle.write(f"{PEPPER_RED_SLUG}\tפלפל אדום\tvegetable\tRAMI_LEVY\tRami Levy\t"
                     f"{PEPPER_SHARED}\tפלפל\t9.9\t\n")
        handle.write(f"{PEPPER_RED_SLUG}\tפלפל אדום\tvegetable\tSHUFERSAL\tShufersal\t"
                     f"{PEPPER_RED_SHUF}\tפלפל אדום\t12.9\t\n")
    with open(map_path, "w", encoding="utf-8") as handle:
        handle.write("slug\tgeneric_key\tprimary\tnote\n")
        handle.write(f"{TOMATO_SLUG}\t{TOMATO_KEY}\tyes\tfixture primary\n")
        handle.write(f"{PEPPER_HOT_SLUG}\t{PEPPER_HOT_KEY}\tyes\tfixture primary\n")
        handle.write(f"{PEPPER_RED_SLUG}\t{PEPPER_RED_KEY}\tyes\tfixture primary\n")
    return units_path, map_path


def build_app_db_from_fixture(prices_db_path, out_path, tmp_path):
    """Run build_app_db.build() against the fixture, pointed at this
    module's own small produce TSVs (not the real checked-in ones)."""
    build_app_db = _module("build_app_db")
    units_path, map_path = write_produce_tsvs(str(tmp_path))
    orig_units = build_app_db.PRODUCE_UNITS_TSV
    orig_map = build_app_db.PRODUCE_GENERIC_MAP_TSV
    build_app_db.PRODUCE_UNITS_TSV = units_path
    build_app_db.PRODUCE_GENERIC_MAP_TSV = map_path
    try:
        counts, _size, _elapsed, _merged_total = build_app_db.build(
            prices_db_path, out_path)
        return counts
    finally:
        build_app_db.PRODUCE_UNITS_TSV = orig_units
        build_app_db.PRODUCE_GENERIC_MAP_TSV = orig_map


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
    generic_entries, of_barcode = build_catalog.write_generics(conn, out_dir)
    conn.close()

    for barcode, key in of_barcode.items():
        entry = entries.get(barcode)
        if entry is not None:
            entry["g"] = key

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

    return entries, of_barcode


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
    with open(os.path.join(out_dir, "generics.json"), encoding="utf-8") as handle:
        generics = json.load(handle)
    return index, barcodes, detail, promo, generics
