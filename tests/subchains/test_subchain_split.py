"""Splitting one published ChainID into the brands that actually price apart.

Netiv Hesed publishes three separately-priced brands - נתיב החסד, בר-כל and
שירה מרקט - under the single ChainID 7290058160839. The database is built on
the assumption that a chain prices chain-wide: one baseline per (chain,
barcode), plus the branches that genuinely differ. That assumption does not
hold here. שירה מרקט's branches agree with each other on 99% of shared
barcodes and disagree with a נתיב החסד branch on about 40% of them, so a
single baseline would be a בר-כל price - carried by that brand's 45 branches
against שירה מרקט's 9 - displayed for a chain where nobody pays it.

So SUBCHAIN_SPLITS gives each brand its own chain id. Two things about that are
worth guarding.

The first is where the sub-chain is read from. A price file states it plainly
and load_prices uses it. A *promotion* file does not: every one of Netiv
Hesed's PromoFull dumps declares ``<SubChainID>000</SubChainID>`` while the
PriceFull for the same branch declares the real 009. Believe that field and
every promotion in the chain lands on a fourth, nameless chain. Reading the
filename instead is no better - dumps.py carries the list of chains whose names
lie about this exact field. The branch is the thing that knows, and load_stores
has already recorded which brand each branch is.

The second is what happens to a brand nobody has listed yet. A chain that adds
one must not have its rows silently attributed to a sibling.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import build_chain_db  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

NETIV = "7290058160839"
SHIRA = f"{NETIV}-009"


class TestSplitId:
    """Which chain id a row belongs to, given the sub-chain it declares."""

    def test_a_listed_subchain_becomes_its_own_chain(self):
        assert build_chain_db.split_id(NETIV, "009") == SHIRA

    def test_the_id_is_padded_the_way_the_files_write_it(self):
        # The chains are inconsistent about zero-padding this field between
        # the store file and the price files.
        assert build_chain_db.split_id(NETIV, "9") == SHIRA

    def test_an_unlisted_subchain_keeps_the_parent_id(self):
        # A brand the chain has added since. It surfaces as one extra chain in
        # the picker - a visible prompt to list it - rather than being folded
        # into a sibling's prices.
        assert build_chain_db.split_id(NETIV, "011") == NETIV

    def test_a_chain_that_is_not_split_is_left_alone(self):
        # Shufersal's שלי / דיל / אקספרס share one price list. Splitting those
        # would turn one chain into three in the picker for nothing.
        assert build_chain_db.split_id("7290027600007", "001") == "7290027600007"


class TestPromotionAttribution:
    """A promotion is attributed through its branch, not its own header.

    The fixture is a real שירה מרקט dump, carved with its header intact: the
    filename says ``-009-`` and the document inside it says
    ``<SubChainID>000</SubChainID>``.
    """

    def build(self, tmp_path, store_rows):
        conn = build_chain_db.connect(str(tmp_path / "chain.db"))
        conn.executemany(
            "INSERT INTO stores (chain_id, store_id, subchain_id, store_name) "
            "VALUES (?,?,?,?)", store_rows)
        build_chain_db.load_promos(
            conn, os.path.join(FIXTURES, "netiv_hesed_shira_branch"), "NETIV_HASED")
        return conn

    def test_the_offers_land_on_the_brand_that_published_them(self, tmp_path):
        conn = self.build(tmp_path, [(SHIRA, "300", "009", "שירה מרקט גבעת שמואל")])
        chains = [row[0] for row in
                  conn.execute("SELECT DISTINCT chain_id FROM promo_offers")]
        assert chains == [SHIRA]

    def test_the_offers_are_not_lost_on_the_way(self, tmp_path):
        conn = self.build(tmp_path, [(SHIRA, "300", "009", "שירה מרקט גבעת שמואל")])
        offers = conn.execute("SELECT COUNT(*) FROM promo_offers").fetchone()[0]
        assert offers > 0

    def test_a_branch_we_have_never_seen_keeps_the_published_id(self, tmp_path):
        # No store row for 300, so nothing can say which brand it is. The
        # parent id is the honest answer; inventing one is not.
        conn = self.build(tmp_path, [(SHIRA, "301", "009", "שירה מרקט פתח תקווה")])
        chains = [row[0] for row in
                  conn.execute("SELECT DISTINCT chain_id FROM promo_offers")]
        assert chains == [NETIV]


class TestSubchainBrands:
    """The branch -> brand map load_promos attributes through."""

    def test_it_is_keyed_on_the_published_chain_id(self, tmp_path):
        conn = build_chain_db.connect(str(tmp_path / "chain.db"))
        conn.execute(
            "INSERT INTO stores (chain_id, store_id, subchain_id, store_name) "
            "VALUES (?,?,?,?)", (SHIRA, "300", "009", "שירה מרקט גבעת שמואל"))
        assert build_chain_db.subchain_brands(conn) == {(NETIV, "300"): SHIRA}

    def test_an_unsplit_chain_contributes_nothing(self, tmp_path):
        conn = build_chain_db.connect(str(tmp_path / "chain.db"))
        conn.execute(
            "INSERT INTO stores (chain_id, store_id, subchain_id, store_name) "
            "VALUES (?,?,?,?)", ("7290027600007", "001", "001", "שופרסל שלי"))
        assert build_chain_db.subchain_brands(conn) == {}
