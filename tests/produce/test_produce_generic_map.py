"""data/produce_generic_map.tsv - the slug<->generic-key map that lets
produce_units.tsv (hand-read) take over from generics.py (algorithmic)
without orphaning a genericKey the app already saved to Firestore.

Every row in the map was decided by reading the candidate's display name,
member count and price range against produce_units.tsv - see
.claude/skills/unify-produce/SKILL.md and the KAN-9 report. This file does
not re-decide anything; it guards the structure and the one thing that must
never silently drift: a key the map references has to be a key the current
generics build actually produces (scripts/build_app_db.py's build_produce
raises loudly on this too - these tests cover the same contract at unit
speed, against a small fixture, per the KAN-9 Definition of Done).
"""

import collections
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402
import generics as generics_mod  # noqa: E402

UNITS = os.path.join(ROOT, "data", "produce_units.tsv")
MAP = os.path.join(ROOT, "data", "produce_generic_map.tsv")

# Slugs in produce_units.tsv that generics.py's own filters (the len(ident)<=2
# cap, the PREPARED regex, the produce_words/stem mismatch for פטריות) never
# gave a generic key to at all in the real merged database - so there is
# nothing for the map to point them at. Confirmed by running
# generics_mod.from_db() against the KAN-6 merged prices.db and finding zero
# candidates for each; see the KAN-9 report's "Noticed, not done" section.
# A slug leaving this set (because generics.py or the upstream data changed)
# should gain a map entry in the same commit that removes it here.
NO_GENERIC_KEY = {
    "mushroom", "potato-red", "potato-white", "grapes", "onion-green",
    "broccoli-kosher", "cabbage-white-kosher",
}


def read_tsv(path):
    with open(path, encoding="utf-8") as handle:
        head = handle.readline().rstrip("\n").split("\t")
        return [dict(zip(head, line.rstrip("\n").split("\t")))
                for line in handle if line.strip()]


@pytest.fixture(scope="module")
def units():
    return read_tsv(UNITS)


@pytest.fixture(scope="module")
def gmap():
    return read_tsv(MAP)


class TestEverySlugIsAccountedFor:
    def test_every_slug_is_mapped_or_documented_absent(self, units, gmap):
        unit_slugs = {r["slug"] for r in units}
        mapped_slugs = {r["slug"] for r in gmap}
        unaccounted = unit_slugs - mapped_slugs - NO_GENERIC_KEY
        assert not unaccounted, (
            f"slugs with no map row and not in NO_GENERIC_KEY: {sorted(unaccounted)} "
            f"- read the candidates (see unify-produce skill) and add rows, or "
            f"confirm there really is no existing generic key and add the slug "
            f"to NO_GENERIC_KEY with why."
        )

    def test_no_generic_key_slugs_do_not_also_appear_in_the_map(self, gmap):
        mapped_slugs = {r["slug"] for r in gmap}
        overlap = mapped_slugs & NO_GENERIC_KEY
        assert not overlap, (
            f"{sorted(overlap)} are both mapped AND listed as having no "
            f"generic key - remove them from NO_GENERIC_KEY, it's stale."
        )

    def test_every_mapped_slug_is_a_real_produce_units_slug(self, units, gmap):
        unit_slugs = {r["slug"] for r in units}
        mapped_slugs = {r["slug"] for r in gmap}
        assert mapped_slugs <= unit_slugs, (
            f"map references slugs not in produce_units.tsv: "
            f"{sorted(mapped_slugs - unit_slugs)}"
        )


class TestExactlyOnePrimaryPerSlug:
    def test_each_mapped_slug_has_exactly_one_primary_key(self, gmap):
        counts = collections.Counter(
            r["slug"] for r in gmap if r["primary"] == "yes")
        all_slugs = {r["slug"] for r in gmap}
        bad = {slug: counts.get(slug, 0) for slug in all_slugs
               if counts.get(slug, 0) != 1}
        assert not bad, f"slugs without exactly one primary key: {bad}"

    def test_primary_column_only_ever_says_yes_or_blank(self, gmap):
        assert {r["primary"] for r in gmap} <= {"yes", ""}


class TestTheFileIsWellFormed:
    def test_no_duplicate_slug_key_pairs(self, gmap):
        pairs = [(r["slug"], r["generic_key"]) for r in gmap]
        assert len(pairs) == len(set(pairs))

    def test_no_key_is_claimed_by_two_different_slugs(self, gmap):
        """A key is one product; two slugs pointing at it would mean two
        different products resolve to the same price row, exactly what the
        skill's tomato/tomato-maggie example says never to do."""
        owner = {}
        clashes = []
        for r in gmap:
            key, slug = r["generic_key"], r["slug"]
            if key in owner and owner[key] != slug:
                clashes.append((key, owner[key], slug))
            owner[key] = slug
        assert not clashes, f"one generic_key mapped to two slugs: {clashes}"

    def test_columns_are_slug_generic_key_primary_note(self):
        with open(MAP, encoding="utf-8") as handle:
            header = handle.readline().rstrip("\n").split("\t")
        assert header == ["slug", "generic_key", "primary", "note"]


class TestMappedKeysMustExistInTheGenericsBuild:
    """build_app_db.build_produce raises loudly at real-build time if a
    mapped key isn't in that build's generics output (see its docstring).
    These tests exercise the same check against a small, controlled fixture
    - see the KAN-9 report for the real check, run against the KAN-6 merged
    prices.db, which is what actually proves the 242 real rows are correct.
    """

    @staticmethod
    def _fixture_conn():
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE chain_products(
                chain_id TEXT, barcode TEXT, name TEXT,
                unit_qty TEXT, unit_of_measure TEXT, is_weighted INTEGER);
            CREATE TABLE chain_prices(
                chain_id TEXT, barcode TEXT, price REAL, store_count INTEGER);
            CREATE TABLE products(barcode TEXT PRIMARY KEY, name TEXT);
        """)
        conn.executemany(
            "INSERT INTO chain_products VALUES (?,?,?,?,?,?)",
            [
                ("RAMI_LEVY", "1001", "עגבניה", None, 'ק"ג', 1),
                ("SHUFERSAL", "1002", "עגבניה", None, 'ק"ג', 1),
            ],
        )
        conn.executemany(
            "INSERT INTO chain_prices VALUES (?,?,?,?)",
            [("RAMI_LEVY", "1001", 6.9, 10), ("SHUFERSAL", "1002", 7.9, 5)],
        )
        conn.executemany(
            "INSERT INTO products VALUES (?,?)",
            [("1001", "עגבניה"), ("1002", "עגבניה")],
        )
        return conn

    def test_the_fixture_reproduces_a_known_key(self):
        # Sanity check on the fixture itself, so a failure below points at
        # build_produce and not at a broken fixture.
        generics, _ = generics_mod.from_db(self._fixture_conn())
        assert "עגבניה" in generics

    def test_build_produce_accepts_a_map_row_pointing_at_a_real_key(self, tmp_path):
        units_tsv = tmp_path / "produce_units.tsv"
        units_tsv.write_text(
            "slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
            "chain_product_name\tprice\tnote\n"
            "tomato\tעגבניה\tveg\tRAMI_LEVY\tRami Levy\t1001\tעגבניה\t6.9\t\n",
            encoding="utf-8",
        )
        map_tsv = tmp_path / "produce_generic_map.tsv"
        map_tsv.write_text(
            "slug\tgeneric_key\tprimary\tnote\ntomato\tעגבניה\tyes\t\n",
            encoding="utf-8",
        )
        counts = build_app_db.build_produce(
            self._fixture_conn(), units_tsv=str(units_tsv), map_tsv=str(map_tsv))
        assert counts == (1, 1, 2, 1)

    def test_build_produce_rejects_a_map_row_pointing_at_an_unknown_key(self, tmp_path):
        units_tsv = tmp_path / "produce_units.tsv"
        units_tsv.write_text(
            "slug\tname_he\tkind\tchain_id\tchain_name\tbarcode\t"
            "chain_product_name\tprice\tnote\n"
            "tomato\tעגבניה\tveg\tRAMI_LEVY\tRami Levy\t1001\tעגבניה\t6.9\t\n",
            encoding="utf-8",
        )
        map_tsv = tmp_path / "produce_generic_map.tsv"
        map_tsv.write_text(
            "slug\tgeneric_key\tprimary\tnote\n"
            "tomato\tלא קיים\tyes\tdeliberately wrong key\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="did not produce"):
            build_app_db.build_produce(
                self._fixture_conn(), units_tsv=str(units_tsv), map_tsv=str(map_tsv))
