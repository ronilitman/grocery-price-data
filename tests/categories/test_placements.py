"""Guards that survive a rebuild.

test_build_categories.py asserts the committed outputs are what the committed
builder produces. That catches a hand-edited file, but it cannot tell a good
change from a disaster: change the mapping so every packaged cheese files as
candy, re-run the builder as you would after any deliberate edit, and it goes
green again. Confirmed by mutating the table and doing exactly that.

So these are the tests that have an opinion of their own.

  - Anchors: twenty real barcodes whose category is not a matter of taste. A
    failure names the product and both categories, so the message says what
    broke rather than "the file changed".
  - The precedence ladder in resolve(), on synthetic paths, so a reordering
    fails on the rule rather than on 38,000 rows of fallout.
  - The merge order across chains. The inversion that let Tiv Taam outrank
    five larger chains shipped because nothing here checked it.
  - Shape: how the catalogue divides between categories and chains, against a
    committed snapshot. Mass misplacement moves these numbers even when every
    row is individually well-formed.

Deliberate changes will fail the shape test - that is the point. Read the
numbers, decide the move is right, and refresh the fixture in the same commit.
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_categories as B  # noqa: E402

# (barcode, expected sub-category slug, the product, so a failure reads)
ANCHORS = [
    ("22", "fresh-veg", "עגבניה"),
    ("964768", "fresh-fruit", "בננה"),
    ("9806571", "poultry", "חזה עוף טרי"),
    ("2868996", "cheese-packaged", "קוטג' 5%"),
    ("522319", "milk-eggs", "חלב טרי מהגולן 3%"),
    ("1258682", "yogurt", "יוגורט עיזים"),
    ("2026", "bread", "לחם אחיד"),
    ("7290121920285", "candy", "שוקולד חלב"),
    ("66318", "salty-snacks", "חטיף במבה"),
    ("7290013585394", "soft-drinks", "קוקה קולה"),
    ("4860019001414", "water", "מים מינרלים"),
    ("176413", "coffee-tea", "קפה נמס"),
    ("7290016096811", "canned", "ממרח טונה"),
    ("211985", "pasta-rice-legumes", "אורז עגול"),
    ("2692782", "oil-vinegar", "תרסיס שמן זית"),
    ("768404001640", "ice-cream", "גלידה קפה"),
    ("7290111348174", "diapers-wipes", "האגיס חיתול לילה"),
    ("7290020495334", "cleaners", "מרסס אקונומיקה"),
    ("7296073724131", "laundry", "אבקת כביסה בייבי"),
    ("7290119020775", "oral", "משחת שיניים פחם"),
]


@pytest.fixture(scope="module")
def taxonomy():
    with open(os.path.join(DATA, "categories.json"), encoding="utf-8") as handle:
        rows = json.load(handle)
    by_id = {r["id"]: r for r in rows}
    return by_id, {r["slug"]: r["id"] for r in rows}


@pytest.fixture(scope="module")
def placed():
    out = {}
    with open(os.path.join(DATA, "product_categories.tsv"), encoding="utf-8") as handle:
        for line in handle:
            barcode, category_id, source = line.rstrip("\n").split("\t")
            out[barcode] = (int(category_id), source)
    return out


class TestKnownProductsLandWhereTheyShould:
    @pytest.mark.parametrize("barcode,slug,product", ANCHORS,
                             ids=[a[1] for a in ANCHORS])
    def test_anchor(self, barcode, slug, product, placed, taxonomy):
        by_id, by_slug = taxonomy
        assert barcode in placed, (
            f"{product} ({barcode}) has no category at all any more - it used to "
            f"be {slug}. Either a chain dropped it or a table key stopped matching."
        )
        got = by_id[placed[barcode][0]]
        assert got["slug"] == slug, (
            f"{product} ({barcode}) is filed as {got['slug']} ({got['name_he']}), "
            f"expected {slug}. Placed from the {placed[barcode][1]} dump."
        )


class TestThePrecedenceLadder:
    """resolve() must try most-specific first. Synthetic paths, no data."""

    def test_a_leaf_override_beats_its_own_pair(self):
        # The real case: seeds shelved with the vegetables they grow into.
        assert B.resolve(["סופרמרקט", "פירות-וירקות", "פירות-וירקות",
                          "זרעי-ירקות-"]) == "ng-garden"
        assert B.resolve(["סופרמרקט", "פירות-וירקות", "פירות-וירקות",
                          "ירקות-טריים"]) == "fresh-veg"

    def test_a_pair_beats_the_whole_department(self):
        # בישום maps department-wide, but a pair under a mapped department
        # must still win where one exists.
        assert B.resolve(["סופרמרקט", "מוצרי-חלב-וביצים", "מדף-הגבינות",
                          "גבינות-צהובות"]) == "cheese-packaged"

    def test_the_department_answers_when_no_pair_does(self):
        assert B.resolve(["פארם-וקוסמטיקה", "בישום", "no-such-category",
                          "no-such-leaf"]) == "fragrance"

    def test_the_storefront_answers_when_nothing_else_does(self):
        assert B.resolve(["צעצועים-ומשחקים", "no-such-dept"]) == "ng-toys"

    def test_an_unknown_path_resolves_to_nothing(self):
        assert B.resolve(["no-such-root", "no-such-dept", "no-such-cat"]) is None
        assert B.resolve([]) is None

    def test_a_promotion_collection_is_not_a_category(self):
        # ...but one that names a real aisle still counts.
        assert B.resolve(["סופרמרקט", "SUPER-SALE", "SUPER-SALE"]) is None
        assert B.resolve(["סופרמרקט", "ראש-השנה", "יין-ואלכוהול"]) == "wine"


@pytest.fixture(scope="module")
def opinions():
    """barcode -> {chain: category_id}, before the merge picks a winner."""
    import chain_maps
    rows, sub_id = B.build_taxonomy()
    out = {}
    shuf, _, _ = B.shufersal(sub_id)
    for barcode, category_id in shuf.items():
        out.setdefault(barcode, {})["shufersal"] = category_id
    for chain, table in chain_maps.BY_CHAIN.items():
        path = os.path.join(DATA, "chain_taxonomies", f"{chain}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)["products"]
        for barcode, product in raw.items():
            key = ">".join(B.path_of(product)[:2])
            slug = table.get(key)
            if slug:
                out.setdefault(barcode, {})[chain] = sub_id[slug]
    for barcode, category_id in B.tiv_taam(sub_id).items():
        out.setdefault(barcode, {}).setdefault("tiv_taam", category_id)
    return out


class TestChainsOutrankEachOtherInOrder:
    def test_shufersal_wins_every_disagreement(self, opinions, placed):
        losses = [b for b, v in opinions.items()
                  if "shufersal" in v and len(v) > 1
                  and b in placed and placed[b][1] != "shufersal"]
        assert not losses, (
            f"{len(losses)} barcodes were placed by another chain even though "
            f"Shufersal had an opinion, e.g. {losses[:3]}"
        )

    def test_tiv_taam_loses_to_the_larger_chains(self, opinions, placed):
        # The bug this exists for: seeding Tiv Taam first and using setdefault
        # for everyone else let the narrowest catalogue of the seven win.
        losses = [b for b, v in opinions.items()
                  if "tiv_taam" in v and len(v) > 1
                  and b in placed and placed[b][1] == "tiv_taam"]
        assert not losses, (
            f"Tiv Taam won {len(losses)} barcodes another chain also places, "
            f"e.g. {losses[:3]} - the merge order has inverted again"
        )


@pytest.fixture(scope="module")
def expected():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "expected_shape.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class TestTheShapeOfTheCatalogue:
    """Mass misplacement moves these even when every row is well-formed."""

    def test_the_row_count_has_not_collapsed(self, placed, expected):
        before, now = expected["total_rows"], len(placed)
        assert now > before * 0.9, (
            f"{before:,} placed barcodes became {now:,}. A chain's table has "
            f"probably stopped matching its dump."
        )

    def test_no_category_has_moved_by_more_than_a_fifth(self, placed, taxonomy,
                                                        expected):
        by_id, _ = taxonomy
        import collections
        now = collections.Counter(
            by_id[by_id[c]["parent_id"]]["slug"] for c, _ in placed.values()
        )
        moved = []
        for slug, was in expected["by_top_category"].items():
            got = now.get(slug, 0)
            if abs(got - was) > max(50, was * 0.2):
                moved.append(f"{slug}: {was:,} -> {got:,}")
        assert not moved, (
            "categories changed size sharply:\n  " + "\n  ".join(moved) +
            "\nIf that is the change you meant, refresh "
            "tests/categories/fixtures/expected_shape.json in the same commit."
        )

    def test_every_chain_still_carries_its_share(self, placed, expected):
        import collections
        now = collections.Counter(src for _, src in placed.values())
        dropped = [f"{chain}: {was:,} -> {now.get(chain, 0):,}"
                   for chain, was in expected["by_source_chain"].items()
                   if now.get(chain, 0) < was * 0.8]
        assert not dropped, (
            "chains lost most of their rows:\n  " + "\n  ".join(dropped)
        )


class TestTheAuditIsClean:
    def test_no_table_key_is_dead_or_points_at_nothing(self):
        _, sub_id = B.build_taxonomy()
        problems = B.audit(sub_id)
        assert not problems, (
            "the mapping tables and the dumps disagree:\n  " +
            "\n  ".join(problems[:10])
        )

    def test_the_builder_runs_without_stack_traces(self):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "build_categories.py")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr[-1500:]
