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
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_app_db  # noqa: E402
import check_produce_tier  # noqa: E402
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
    """A mapped key can go stale on its own - a chain renames or drops a
    weighed item and generics.py's grouping shifts, with no edit to this
    repo. build_app_db.build_produce (see its docstring) skips a stale
    NON-primary row (recording it in meta['produce_map_stale']) so a nightly
    build never stops over it, but still raises loudly if a slug's PRIMARY
    key goes stale - that is the one key grocery-list-app's genericKey
    resolution actually depends on. These tests exercise both paths against
    a small, controlled fixture - see the KAN-9 report for the real check,
    run against the KAN-6 merged prices.db.
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
            -- Normally created by build()'s main SCHEMA before build_produce
            -- runs; supplied directly here since these tests call
            -- build_produce in isolation.
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
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
        # (produce_units, generics, generic_members, produce_generic_map,
        # generic_barcodes) - the last is KAN-13's post-merge fix: the full
        # `b` list for the one key here spans both RAMI_LEVY's and
        # SHUFERSAL's barcodes (1001, 1002).
        assert counts == (1, 1, 2, 1, 2)

    def test_a_stale_primary_key_raises(self, tmp_path):
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
        with pytest.raises(ValueError, match="PRIMARY key"):
            build_app_db.build_produce(
                self._fixture_conn(), units_tsv=str(units_tsv), map_tsv=str(map_tsv))

    def test_a_stale_non_primary_key_is_skipped_and_recorded(self, tmp_path, capsys):
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
            "tomato\tעגבניה\tyes\t\n"
            "tomato\tלא קיים\t\tchain renamed this one\n",
            encoding="utf-8",
        )
        conn = self._fixture_conn()
        counts = build_app_db.build_produce(
            conn, units_tsv=str(units_tsv), map_tsv=str(map_tsv))
        # The primary row still loaded; the stale non-primary one did not,
        # so the build kept going instead of raising.
        assert counts == (1, 1, 2, 1, 2)

        warning = capsys.readouterr().out
        assert "WARNING" in warning and "לא קיים" in warning

        stale_json = conn.execute(
            "SELECT value FROM meta WHERE key='produce_map_stale'").fetchone()[0]
        assert json.loads(stale_json) == [{"slug": "tomato", "generic_key": "לא קיים"}]

    def test_no_stale_rows_records_an_empty_list_in_meta(self, tmp_path):
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
        conn = self._fixture_conn()
        build_app_db.build_produce(conn, units_tsv=str(units_tsv), map_tsv=str(map_tsv))
        stale_json = conn.execute(
            "SELECT value FROM meta WHERE key='produce_map_stale'").fetchone()[0]
        assert json.loads(stale_json) == []


class TestPriceTiersStayHonest:
    """The owner's rule: a price tier is a product. A non-primary key priced
    2x+ away from its slug's primary is probably a different product wearing
    the same generic key (organic, a pricier cultivar, sold by weight) -
    KAN-9's review found 33 real examples of this. scripts/check_produce_tier.py
    is what catches it against a real app.db; these tests exercise the same
    logic (find_tier_violations) against a small fixture, so CI still fails
    if a future edit reintroduces a silent price-tier mismatch, and passes
    once the row is either removed or given a `tier-ok:` note explaining why
    it really is the same product.
    """

    @staticmethod
    def _fixture_conn(prices):
        """prices: {(key, chain_id, barcode): price}."""
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE generic_members(key TEXT, chain_id TEXT, barcode TEXT);
            CREATE TABLE chain_prices(chain_id TEXT, barcode TEXT, price REAL);
        """)
        for (key, chain_id, barcode), price in prices.items():
            conn.execute("INSERT INTO generic_members VALUES (?,?,?)",
                         (key, chain_id, barcode))
            conn.execute("INSERT INTO chain_prices VALUES (?,?,?)",
                         (chain_id, barcode, price))
        conn.commit()
        return conn

    def test_a_non_primary_key_far_from_the_primary_without_a_note_is_flagged(self):
        conn = self._fixture_conn({
            ("plain", "C1", "1"): 6.9,
            ("organic", "C2", "2"): 15.9,  # 2.3x, no tier-ok note
        })
        gmap = [
            {"slug": "cucumber", "generic_key": "plain", "primary": "yes", "note": ""},
            {"slug": "cucumber", "generic_key": "organic", "primary": "", "note": "organic"},
        ]
        violations = check_produce_tier.find_tier_violations(conn, gmap)
        assert violations == [("cucumber", "organic", round(15.9 / 6.9, 2))]

    def test_the_same_gap_with_a_tier_ok_note_is_not_flagged(self):
        conn = self._fixture_conn({
            ("plain", "C1", "1"): 6.9,
            ("organic", "C2", "2"): 15.9,
        })
        gmap = [
            {"slug": "cucumber", "generic_key": "plain", "primary": "yes", "note": ""},
            {"slug": "cucumber", "generic_key": "organic", "primary": "",
             "note": "tier-ok: confirmed via produce_units X row"},
        ]
        assert check_produce_tier.find_tier_violations(conn, gmap) == []

    def test_a_key_within_two_x_is_never_flagged_even_without_a_note(self):
        conn = self._fixture_conn({
            ("plain", "C1", "1"): 6.9,
            ("close", "C2", "2"): 12.9,  # 1.87x - inside the 2x/0.5x guard band
        })
        gmap = [
            {"slug": "cucumber", "generic_key": "plain", "primary": "yes", "note": ""},
            {"slug": "cucumber", "generic_key": "close", "primary": "", "note": ""},
        ]
        assert check_produce_tier.find_tier_violations(conn, gmap) == []

    def test_the_real_checked_in_map_has_no_unexplained_tier_gap_in_this_fixture(self):
        """Not a substitute for running check_produce_tier.py against a real
        app.db (see the KAN-9 report) - this only proves the checked-in TSV's
        rows are self-consistent: every row flagged in the KAN-9 review either
        isn't in the file any more, or carries a `tier-ok:` note. Real member
        prices aren't available in this repo, so a live database can't be
        joined here.
        """
        gmap = read_tsv(MAP)
        by_slug_key = {(r["slug"], r["generic_key"]): r for r in gmap}
        # The 8 rows the KAN-9 review found genuinely tier-divergent but
        # confirmed via an exact produce_units chain+barcode match.
        expected_tier_ok = {
            ("apple", "בחוץ תפוח"),
            ("beetroot", "אורגני סלק"),
            ("broccoli", "ברוקולי תפזורת"),
            ("lemon", "4 9 לימון"),
            ("lemon", "לימון שקיל"),
            ("lettuce", "חסה צבעונית"),
            ("persimmon", "אפרסמון גדול"),
            ("persimmon", "אפרסמון מובחר"),
        }
        for slug_key in expected_tier_ok:
            assert slug_key in by_slug_key, f"{slug_key} was removed - update this test too"
            assert "tier-ok:" in by_slug_key[slug_key]["note"], (
                f"{slug_key} lost its tier-ok note"
            )
        # The 25 rows the review found unconfirmed and removed must stay gone.
        removed = {
            ("apple", "פרימיום תפוח"), ("avocado", "BL אבוקדו"),
            ("carrot", "10 גזר"), ("carrot", "אורגני גזר"), ("carrot", "גזר צבעוני"),
            ("cauliflower", "כרובית צבעונית"), ("clementine", "אורגנית קלמנטינה"),
            ("cucumber", "אורגני מלפפון"), ("cucumber", "מלפפון פקוס"),
            ("garlic-fresh", "צעיר שום"), ("garlic-fresh", "שום"),
            ("lemon", "אורגני לימון"), ("lettuce", "חסה סלנובה"),
            ("mango", "במשקל מנגו"), ("melon", "אורגני מלון"), ("melon", "מלון שרנטה"),
            ("orange", "סיני תפוז"), ("pear", "55 אגס ספדונה"),
            ("persimmon", "70 אפרסמון"), ("pumpkin", "אורגנית דלעת"),
            ("quince", "במשקל חבוש"), ("sweet-potato", "בטטה מתוקה"),
            ("sweet-potato", "בטטה קוסביה"), ("tomato", "לבישול עגבניה"),
            ("watermelon", "אבטיח אורגני"),
        }
        reintroduced = removed & set(by_slug_key)
        assert not reintroduced, (
            f"previously-removed unconfirmed price-tier rows are back: "
            f"{reintroduced} - re-read them (see the KAN-9 report) before "
            f"re-adding, and add a tier-ok note if they really do belong"
        )
