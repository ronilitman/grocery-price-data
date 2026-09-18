"""build_catalog.py applies the KAN-29 decided-name table too (the gap this
closes: the names reached app.db/the API but not the GitHub Pages shards the
app actually reads by default - src/prices.js in grocery-list-app).

build_catalog.build_entries() is the single site both the per-barcode shard
entries ("n") and the name-search index (write_names, which reads entry["n"])
take a product name from, so one fixed lookup there covers both. These are
unit tests, same shape as tests/app_db/test_product_names.py: build_entries
and write_names are exercised directly against tiny fixtures, not through the
full main()/CLI pipeline.
"""

import json
import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402
import build_catalog  # noqa: E402

REAL_NAMES_JSON = build_app_db.PRODUCT_NAMES_JSON

# The barcode from the bug report: the API's decided name for it is settled
# in the real, checked-in table (also asserted by
# test_product_names.py::test_real_json_has_the_two_required_barcodes_settled).
DECIDED_BARCODE = "7290011723200"
DECIDED_NAME = "טחינה גולמית הר ברכה תעשיות 500 גרם"
OLD_SHARD_NAME = "*מבצע* טחינה הר ברכה"

UNREVIEWED_BARCODE = "5555500000000"
UNREVIEWED_NAME = "Some Unreviewed Product"


def _products_conn(rows):
    """An in-memory `products` table shaped like prices.db's, for
    build_entries to SELECT from - same fixture pattern as
    test_product_names.py's _src_db."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE products(barcode TEXT PRIMARY KEY, name TEXT, "
        "unit_qty TEXT, is_weighted INTEGER)"
    )
    conn.executemany("INSERT INTO products VALUES (?,?,?,?)", rows)
    conn.commit()
    return conn


def test_barcode_in_table_gets_decided_name_in_shard_entry():
    conn = _products_conn([(DECIDED_BARCODE, OLD_SHARD_NAME, "500", 0)])
    name_map = {DECIDED_BARCODE: DECIDED_NAME}

    entries = build_catalog.build_entries(conn, name_map)

    assert entries[DECIDED_BARCODE]["n"] == DECIDED_NAME


def test_barcode_not_in_table_is_unchanged():
    conn = _products_conn([(UNREVIEWED_BARCODE, UNREVIEWED_NAME, "1", 0)])
    name_map = {DECIDED_BARCODE: DECIDED_NAME}  # unrelated barcode

    entries = build_catalog.build_entries(conn, name_map)

    assert entries[UNREVIEWED_BARCODE]["n"] == UNREVIEWED_NAME


def test_no_name_map_at_all_falls_back_for_everything():
    conn = _products_conn([(UNREVIEWED_BARCODE, UNREVIEWED_NAME, "1", 0)])

    entries = build_catalog.build_entries(conn, name_map=None)

    assert entries[UNREVIEWED_BARCODE]["n"] == UNREVIEWED_NAME


def test_decided_name_is_what_gets_indexed_for_search(tmp_path):
    conn = _products_conn([(DECIDED_BARCODE, OLD_SHARD_NAME, "500", 0)])
    name_map = {DECIDED_BARCODE: DECIDED_NAME}
    entries = build_catalog.build_entries(conn, name_map)

    out_dir = str(tmp_path)
    build_catalog.write_names(entries, out_dir)

    # "גולמית" is only in the decided name; "מבצע" is only in the old one.
    with open(os.path.join(out_dir, "name", "index.json"), encoding="utf-8") as h:
        index = json.load(h)
    term = "גולמית"
    prefix = next(p for p in index["shards"] if term.startswith(p))
    with open(os.path.join(out_dir, "name", f"{prefix}.json"), encoding="utf-8") as h:
        rows = json.load(h)

    names_in_shard = {row[1] for row in rows}
    assert DECIDED_NAME in names_in_shard
    assert OLD_SHARD_NAME not in names_in_shard
    assert not any("מבצע" in name for name in names_in_shard)


def test_real_decided_name_round_trips_byte_for_byte():
    """The real table's name reaches the shard intact - whatever it says today.

    Deliberately not asserted against a fixed string: a decided name is
    re-decided when better evidence arrives (this barcode's was, once), and
    pinning the literal here turns that improvement into a red build. What
    must hold is that the shard carries the table's name byte for byte, and
    that it is not the old truncated one.
    """
    conn = _products_conn([(DECIDED_BARCODE, OLD_SHARD_NAME, "500", 0)])
    name_map = build_app_db.load_product_names(REAL_NAMES_JSON)
    decided = name_map[DECIDED_BARCODE]

    entries = build_catalog.build_entries(conn, name_map)
    got = entries[DECIDED_BARCODE]["n"]

    assert [ord(c) for c in got] == [ord(c) for c in decided]
    assert got.encode("utf-8") == decided.encode("utf-8")
    assert got != OLD_SHARD_NAME
