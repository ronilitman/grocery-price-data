"""KAN-9: build_app_db.build() now always loads produce_units.tsv and
produce_generic_map.tsv (build_produce, called once from build()). This
directory's fixtures (test_build_app_db.py, test_deals.py - KAN-6/KAN-7;
test_fts.py - KAN-8) are tiny synthetic databases with no loose produce in
them at all, so none of the checked-in map's ~54 slugs would find their
generic key in a build from one of these fixtures. build_produce tolerates a
stale NON-primary key (skips it, records it in meta), but still raises
loudly when a slug's PRIMARY key goes missing - and with a tiny fixture,
every mapped slug's primary goes missing at once, so the first one still
crashes the build.

Point PRODUCE_UNITS_TSV/PRODUCE_GENERIC_MAP_TSV at empty (header-only) files
for every test under this directory, so KAN-6/7/8's fixtures keep testing
what they've always tested without needing to know KAN-9 exists. Real
coverage of the checked-in produce data lives in tests/produce/ and in the
KAN-9 report's real-database build.
"""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402


@pytest.fixture(autouse=True)
def empty_produce_inputs(tmp_path, monkeypatch):
    units_tsv = tmp_path / "empty_produce_units.tsv"
    units_tsv.write_text(
        "slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
        "chain_product_name\tprice\tnote\n", encoding="utf-8")
    map_tsv = tmp_path / "empty_produce_generic_map.tsv"
    map_tsv.write_text("slug\tgeneric_key\tprimary\tnote\n", encoding="utf-8")
    monkeypatch.setattr(build_app_db, "PRODUCE_UNITS_TSV", str(units_tsv))
    monkeypatch.setattr(build_app_db, "PRODUCE_GENERIC_MAP_TSV", str(map_tsv))
