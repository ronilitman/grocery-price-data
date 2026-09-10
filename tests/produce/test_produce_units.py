"""Loose produce stays unified, and a new chain does not slip past unnoticed.

data/produce_units.tsv is rebuilt by hand from a merged database that is not in
the repo, so nothing regenerates it on a nightly build. That makes it the kind
of file that quietly rots: a chain joins, its tomatoes are simply absent, and
every other check still passes because every row that *is* there is fine.

So two kinds of guard. The first is about the produce itself - the two dozen
things people actually put on a list have to be in most chains, because a
tomato is not a rarity. The second is about the pipeline: if the build's chain
matrix has changed since this file was last built, the fixture no longer
matches and the failure says to run the skill again.
"""

import json
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
DATA = os.path.join(ROOT, "data")
UNITS = os.path.join(DATA, "produce_units.tsv")
CHAINS = os.path.join(DATA, "produce_chains.tsv")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "build_matrix_chains.json")
SKILL = "unify-produce"

# The things a household buys most weeks. Each has to be priced by most of the
# chains that sell produce at all - if one of these is thin, the unification
# has drifted rather than the country having stopped eating cucumbers.
CORE = [
    "tomato", "cucumber", "onion", "garlic", "carrot", "potato", "sweet-potato",
    "pepper", "eggplant", "zucchini", "cabbage", "cauliflower", "lemon",
    "apple", "banana", "orange", "clementine", "avocado", "watermelon", "melon",
    "grapes", "pear", "mushroom", "corn", "pumpkin",
]
# Not a majority: some chains sell only banana chips and no bananas, and some
# stock no loose produce worth the name. The threshold is set to catch drift,
# not to assert that every chain is a greengrocer.
MIN_SHARE = 0.4


def read(path):
    with open(path, encoding="utf-8") as handle:
        head = handle.readline().rstrip("\n").split("\t")
        return [dict(zip(head, line.rstrip("\n").split("\t"))) for line in handle]


@pytest.fixture(scope="module")
def units():
    return read(UNITS)


@pytest.fixture(scope="module")
def chains():
    return read(CHAINS)


class TestTheCommonProduceIsEverywhere:
    @pytest.mark.parametrize("slug", CORE)
    def test_core_item_is_priced_by_most_chains(self, slug, units, chains):
        here = {r["chain_id"] for r in units if r["slug"] == slug}
        assert here, (
            f"{slug} is not unified at all any more. Rebuild the merged "
            f"database and re-run the {SKILL} skill."
        )
        share = len(here) / len(chains)
        assert share >= MIN_SHARE, (
            f"{slug} is only priced by {len(here)} of {len(chains)} chains "
            f"({share:.0%}). Something people buy every week should be in most "
            f"of them - check produce_spec.py's entry before accepting this."
        )


class TestEveryChainCarriesProduce:
    def test_no_chain_in_the_file_is_nearly_empty(self, units, chains):
        counts = {c["chain_id"]: int(c["products"]) for c in chains}
        # Yellow is a forecourt shop and Keshet Teamim is largely online;
        # four is "this chain sells produce at all", not "sells a lot".
        thin = {c: n for c, n in counts.items() if n < 4}
        names = {c["chain_id"]: c["chain_name"] for c in chains}
        assert not thin, (
            "chains with almost no produce:\n  " + "\n  ".join(
                f"{names[c]}: {n} products" for c, n in thin.items())
        )

    def test_the_companion_file_matches_the_units(self, units, chains):
        assert {c["chain_id"] for c in chains} == {r["chain_id"] for r in units}


class TestANewChainCannotSlipPast:
    def test_the_build_matrix_has_not_changed(self):
        with open(os.path.join(ROOT, ".github", "workflows", "build.yml"),
                  encoding="utf-8") as handle:
            live = sorted({n for n in re.findall(
                r"^\s+-\s+([A-Z][A-Z0-9_]{2,})\s*$", handle.read(), re.M)})
        with open(FIXTURE, encoding="utf-8") as handle:
            known = json.load(handle)
        added, gone = sorted(set(live) - set(known)), sorted(set(known) - set(live))
        assert not added and not gone, (
            (f"chains added to the build: {', '.join(added)}\n" if added else "")
            + (f"chains removed: {', '.join(gone)}\n" if gone else "")
            + f"data/produce_units.tsv was built before this change, so the new "
              f"chain's fruit and vegetables are missing from every price "
              f"comparison. Re-run the {SKILL} skill, then refresh "
              f"tests/produce/fixtures/build_matrix_chains.json in the same commit."
        )


class TestTheFileIsWellFormed:
    def test_one_row_per_product_and_chain(self, units):
        keys = [(r["slug"], r["chain_id"]) for r in units]
        assert len(keys) == len(set(keys))

    def test_sorted_by_product_then_chain(self, units):
        keys = [(r["slug"], r["chain_id"]) for r in units]
        assert keys == sorted(keys)

    def test_every_slug_is_in_the_spec(self, units):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from produce_spec import PRODUCE
        known = {e[0] for e in PRODUCE}
        assert {r["slug"] for r in units} <= known

    def test_every_price_is_inside_its_band(self, units):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from produce_spec import PRICE_BAND
        bad = [f"{r['slug']} {r['chain_name']} {r['chain_product_name']} "
               f"at {r['price']}"
               for r in units
               if not (PRICE_BAND[r["slug"]][0] <= float(r["price"])
                       <= PRICE_BAND[r["slug"]][1])]
        assert not bad, "prices outside their band:\n  " + "\n  ".join(bad[:10])

    def test_kinds_are_the_three_we_use(self, units):
        assert {r["kind"] for r in units} <= {"veg", "fruit", "herb"}
