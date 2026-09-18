"""KAN-29: the proposer reads the per-chain databases, not chain_products.

``prices.db``'s ``chain_products`` is weighed-goods only (``merge_db.py``
filters on ``is_weighted = 1``), so every packaged product is missing from it.
These tests build small chain databases of the real shape instead.
"""

import csv
import os
import sqlite3
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

import propose_names  # noqa: E402


def make_chain(root, dirname, chain_id, label, products):
    """products: [(barcode, name, manufacturer)] -> a db of the real shape."""
    folder = os.path.join(root, f"db-{dirname}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{dirname.lower()}.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chains(chain_id TEXT PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE products(barcode TEXT PRIMARY KEY, name TEXT, "
        "manufacturer TEXT, unit_qty TEXT, quantity REAL, "
        "unit_of_measure TEXT, is_weighted INTEGER)")
    conn.execute("INSERT INTO chains VALUES (?,?)", (chain_id, label))
    conn.executemany(
        "INSERT INTO products(barcode, name, manufacturer) VALUES (?,?,?)",
        products)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def chains(tmp_path):
    root = str(tmp_path / "chain_dbs")
    # Three chains publish full names and agree; one disagrees; one caps at
    # exactly 20 characters, which is how a capping chain is detected.
    make_chain(root, "SHUFERSAL", "7290027600007", "שופרסל",
               [("111", "טחינה הר ברכה 500 גרם", "הר ברכה")])
    make_chain(root, "BAREKET", "7290875100001", "סופר ברקת",
               [("111", "טחינה הר ברכה 500 גרם", "")])
    make_chain(root, "TIV_TAAM", "7290873255550", "טיב טעם",
               [("111", "טחינה הר ברכה 100% שומשום טהור", "")])
    make_chain(root, "RAMI_LEVY", "7290058140886", "רמי לוי",
               [("111", "טחינה הר ברכה 500 גר", ""),
                ("222", "מוצר שרק הרשת מוכרת", "")])
    return root


def test_normalize_name_undoes_csv_escaping_but_keeps_real_gershayim():
    # A retailer feed that ran a name through CSV-quoting and never
    # un-quoted it again wraps the whole field in a pair of double quotes
    # and doubles every literal `"` inside - undo exactly that.
    mangled = '"foo ""bar"""'
    assert propose_names.normalize_name(mangled) == 'foo "bar"'

    # A genuine gershayim - a lone `"` with real text on both sides, never
    # doubled or field-wrapping - must survive untouched.
    gershayim = 'טחינה 500 מ"ל'
    got = propose_names.normalize_name(gershayim)
    assert [ord(c) for c in got] == [ord(c) for c in gershayim]


def test_capping_chain_is_derived_from_the_data(chains):
    caps = {label: capped for _id, label, _p, capped in propose_names.chain_dbs(chains)}
    # רמי לוי's longest name is exactly 20 characters, the others' are not.
    assert caps["רמי לוי"] is True
    assert caps["שופרסל"] is False
    assert caps["טיב טעם"] is False


def test_the_most_repeated_full_name_wins(chains):
    found = propose_names.gather(chains)
    name, source, _brand = propose_names.propose_one(found["111"])
    assert name == "טחינה הר ברכה 500 גרם"
    assert source == "trusted"
    # The capped chain's truncated name is collected but does not win.
    assert any(n == "טחינה הר ברכה 500 גר" for _l, n, _m, _c in found["111"])


def test_hebrew_survives_byte_for_byte(chains):
    found = propose_names.gather(chains)
    name, _source, _brand = propose_names.propose_one(found["111"])
    # Verified by codepoint, never by eye.
    assert list(name)[:5] == ["ט", "ח", "י", "נ", "ה"]
    assert len(name) == 21
    assert name.encode("utf-8").decode("utf-8") == name


def test_capping_fallback_when_only_a_capped_chain_stocks_it(chains):
    found = propose_names.gather(chains)
    name, source, _brand = propose_names.propose_one(found["222"])
    assert name == "מוצר שרק הרשת מוכרת"
    assert source == "capping-fallback"


def test_a_tie_takes_the_longest_and_says_so(chains):
    candidates = [
        ("שופרסל", "קצר", "", False),
        ("טיב טעם", "שם ארוך יותר", "", False),
    ]
    name, source, _brand = propose_names.propose_one(candidates)
    assert name == "שם ארוך יותר"
    assert "tie-longest" in source


def test_barcode_filter_only_reads_what_was_asked_for(chains):
    found = propose_names.gather(chains, barcodes=["222"])
    assert set(found) == {"222"}


def test_already_decided_barcodes_are_skipped(chains, tmp_path):
    names_tsv = tmp_path / "product_names.tsv"
    names_tsv.write_text(
        "# a comment line is skipped\n"
        "111\tטחינה גולמית הר ברכה 500 גרם\tpricez\t2026-09-17\n",
        encoding="utf-8")
    existing = propose_names.load_existing(str(names_tsv))
    assert existing == {"111"}
    found = propose_names.gather(chains)
    proposed = [row[0] for row in propose_names.propose(found, existing)]
    assert proposed == ["222"]


def test_review_file_is_a_tsv_a_human_can_check(chains, tmp_path):
    found = propose_names.gather(chains)
    out = tmp_path / "review.tsv"
    propose_names.write_review(str(out), propose_names.propose(found, set()))
    rows = list(csv.DictReader(out.open(encoding="utf-8"), delimiter="\t"))
    row = next(r for r in rows if r["barcode"] == "111")
    assert row["proposed_name"] == "טחינה הר ברכה 500 גרם"
    assert row["chains"] == "4"
    # Every candidate is shown with the chain that filed it, so the reviewer
    # can disagree with the proposal.
    assert "טיב טעם: טחינה הר ברכה 100% שומשום טהור" in row["candidates"]
    assert "רמי לוי: טחינה הר ברכה 500 גר" in row["candidates"]
