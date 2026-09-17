"""scripts/propose_names.py (KAN-29 part 2): a trusted-chain candidate beats
a capping-chain one, a barcode nobody trusted stocks falls back to the
capping chains instead of being dropped, and a barcode already decided in
product_names.tsv is skipped rather than proposed again.
"""

import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import propose_names  # noqa: E402

TRUSTED_A = next(iter(propose_names.TRUSTED_CHAINS))
TRUSTED_B = list(propose_names.TRUSTED_CHAINS)[1]
CAPPING_A = next(iter(propose_names.CAPPING_CHAINS))


def _conn(chain_products_rows, chains_rows=()):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chains(chain_id TEXT PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE chain_products(chain_id TEXT, barcode TEXT, name TEXT)"
    )
    conn.executemany("INSERT INTO chains VALUES (?,?)", chains_rows)
    conn.executemany(
        "INSERT INTO chain_products (chain_id, barcode, name) VALUES (?,?,?)",
        chain_products_rows,
    )
    conn.commit()
    return conn


def test_trusted_chain_candidate_beats_capping_chain():
    conn = _conn([
        (CAPPING_A, "1111111111111", "*מבצע* שם קטוע 20 תו"),
        (TRUSTED_A, "1111111111111", "השם המלא והנכון"),
    ])
    by_barcode = propose_names.gather_candidates(conn)

    [(barcode, name, source, rows)] = list(propose_names.propose(by_barcode, existing=set()))

    assert barcode == "1111111111111"
    assert name == "השם המלא והנכון"
    assert source == "trusted"
    assert len(rows) == 2  # every candidate is still reported, not just the winner


def test_capping_chain_is_a_fallback_when_no_trusted_chain_stocks_it():
    conn = _conn([
        (CAPPING_A, "2222222222222", "שם קטוע ל-20 תווים"),
    ])
    by_barcode = propose_names.gather_candidates(conn)

    [(barcode, name, source, rows)] = list(propose_names.propose(by_barcode, existing=set()))

    assert name == "שם קטוע ל-20 תווים"
    assert source == "capping-fallback"


def test_most_common_trusted_name_wins_a_tie():
    conn = _conn([
        (TRUSTED_A, "3333333333333", "שם א"),
        (TRUSTED_B, "3333333333333", "שם ב"),
        # a third trusted row agreeing with TRUSTED_A's name
        (CAPPING_A, "3333333333333", "שם ב"),
    ])
    # Make the tie unambiguous: two *trusted* rows say "שם א".
    conn.execute(
        "INSERT INTO chain_products (chain_id, barcode, name) VALUES (?,?,?)",
        (TRUSTED_B, "3333333333333", "שם א"),
    )
    by_barcode = propose_names.gather_candidates(conn)

    [(_, name, source, _)] = list(propose_names.propose(by_barcode, existing=set()))

    assert name == "שם א"
    assert source == "trusted"


def test_already_decided_barcode_is_skipped():
    conn = _conn([
        (TRUSTED_A, "4444444444444", "כבר הוחלט"),
    ])
    by_barcode = propose_names.gather_candidates(conn)

    proposals = list(propose_names.propose(by_barcode, existing={"4444444444444"}))

    assert proposals == []


def test_load_existing_barcodes_skips_comments_and_blank_lines(tmp_path):
    tsv_path = tmp_path / "product_names.tsv"
    tsv_path.write_text(
        "# a header comment\n"
        "\n"
        "1111111111111\tName\tsource\t2026-09-17\n",
        encoding="utf-8",
    )

    assert propose_names.load_existing_barcodes(str(tsv_path)) == {"1111111111111"}
