"""scripts/assign_ids.py: chain_uid/branch_uid must never move once given out.

KAN-34 gives every chain and branch an id we control (a uuid4 string), kept
in data/chain_ids.json and data/branch_ids.json. The one property that makes
those files trustworthy is stability: the same input produces the same ids
every time, a chain or branch that vanishes from the input keeps its old id
rather than losing it, and a new one gets a fresh id without disturbing
anything already assigned. These tests exercise exactly that against small
STORE_FILE-shaped fixtures - not the real dumps, which scripts/assign_ids.py
was verified against separately.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import assign_ids  # noqa: E402

CHAIN_A = "7290000000001"
CHAIN_B = "7290000000002"


def store_file(chain_id, chain_name, stores):
    """A minimal STORE_FILE document: root > SubChains > SubChain > Stores > Store."""
    store_xml = "".join(
        f"<Store><StoreID>{store_id}</StoreID><BikoretNo>1</BikoretNo>"
        f"<StoreType>1</StoreType><StoreName>{name}</StoreName>"
        f"<Address>Some St 1</Address><City>{city}</City>"
        f"<ZIPCode>1234567</ZIPCode></Store>"
        for store_id, name, city in stores
    )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Chain><ChainID>{chain_id}</ChainID><ChainName>{chain_name}</ChainName>"
        "<SubChains><SubChain><SubChainID>001</SubChainID>"
        "<SubChainName>1</SubChainName>"
        f"<Stores>{store_xml}</Stores></SubChain></SubChains></Chain>"
    ).encode("utf-8")


def write_dump(dumps_dir, chain_dir, filename, xml_bytes):
    chain_path = os.path.join(dumps_dir, chain_dir)
    os.makedirs(chain_path, exist_ok=True)
    with open(os.path.join(chain_path, filename), "wb") as f:
        f.write(xml_bytes)


def run(dumps_dir, chain_ids_path, branch_ids_path):
    return assign_ids.assign_with_paths(dumps_dir, chain_ids_path, branch_ids_path)


class TestIdempotence:
    def test_running_twice_changes_nothing(self, tmp_path):
        dumps = tmp_path / "dumps"
        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "Store One", "3000"),
                                                    ("002", "Store Two", "5000")]))
        chain_ids_path = tmp_path / "chain_ids.json"
        branch_ids_path = tmp_path / "branch_ids.json"

        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        first_chain = chain_ids_path.read_bytes()
        first_branch = branch_ids_path.read_bytes()

        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        assert chain_ids_path.read_bytes() == first_chain
        assert branch_ids_path.read_bytes() == first_branch


class TestNewBranch:
    def test_a_new_branch_gets_a_fresh_id_without_touching_the_others(self, tmp_path):
        dumps = tmp_path / "dumps"
        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "Store One", "3000")]))
        chain_ids_path = tmp_path / "chain_ids.json"
        branch_ids_path = tmp_path / "branch_ids.json"
        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        before = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]

        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "Store One", "3000"),
                                                    ("002", "Store Two", "5000")]))
        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        after = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]

        assert set(after) == {f"{CHAIN_A}|1", f"{CHAIN_A}|2"}
        assert after[f"{CHAIN_A}|1"] == before[f"{CHAIN_A}|1"]  # untouched


class TestRemovedBranchKeepsItsId:
    def test_a_branch_missing_from_the_input_is_not_dropped_or_renumbered(self, tmp_path):
        dumps = tmp_path / "dumps"
        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "Store One", "3000"),
                                                    ("002", "Store Two", "5000")]))
        chain_ids_path = tmp_path / "chain_ids.json"
        branch_ids_path = tmp_path / "branch_ids.json"
        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        before = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]

        # Store 002 stops showing up - a closed branch, or just a day the
        # chain's file did not carry it.
        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "Store One", "3000")]))
        run(str(dumps), str(chain_ids_path), str(branch_ids_path))
        after = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]

        # Still there, still the same id - never reclaimed.
        assert after[f"{CHAIN_A}|2"] == before[f"{CHAIN_A}|2"]
        assert after[f"{CHAIN_A}|1"] == before[f"{CHAIN_A}|1"]


class TestChainAndBranchDiscovery:
    def test_two_chains_and_their_branches_are_all_found(self, tmp_path):
        dumps = tmp_path / "dumps"
        write_dump(str(dumps), "ChainA", "Stores1.xml",
                   store_file(CHAIN_A, "Chain A", [("001", "A1", "3000")]))
        write_dump(str(dumps), "ChainB", "Stores1.xml",
                   store_file(CHAIN_B, "Chain B", [("001", "B1", "5000"),
                                                    ("002", "B2", "0")]))
        chain_ids_path = tmp_path / "chain_ids.json"
        branch_ids_path = tmp_path / "branch_ids.json"
        new_chains, total_chains, new_branches, total_branches = run(
            str(dumps), str(chain_ids_path), str(branch_ids_path))

        assert (new_chains, total_chains) == (2, 2)
        assert (new_branches, total_branches) == (3, 3)
        branch_ids = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]
        assert set(branch_ids) == {
            f"{CHAIN_A}|1", f"{CHAIN_B}|1", f"{CHAIN_B}|2",
        }


class TestNestedWrapperShape:
    """City Market's .NET serialisation adds extra wrapper layers and names
    the store record ``SubChainStoreXMLObject`` instead of ``Store``."""

    def test_a_store_nested_under_extra_generic_wrappers_is_still_found(self, tmp_path):
        xml = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<root><ChainId>{CHAIN_A}</ChainId><ChainName>Chain A</ChainName>"
            "<SubChains><SubChainsXMLObject><SubChain>"
            "<SubChainId>001</SubChainId>"
            "<Stores><SubChainStoresXMLObject><Store>"
            "<SubChainStoreXMLObject><StoreId>027</StoreId><BikoretNo>1</BikoretNo>"
            "<StoreType>1</StoreType><StoreName>Nested Store</StoreName>"
            "<Address>unknown</Address><City>unknown</City><ZipCode>unknown</ZipCode>"
            "</SubChainStoreXMLObject>"
            "</Store></SubChainStoresXMLObject></Stores>"
            "</SubChain></SubChainsXMLObject></SubChains></root>"
        ).encode("utf-8")
        dumps = tmp_path / "dumps"
        write_dump(str(dumps), "ChainA", "Stores1.xml", xml)
        chain_ids_path = tmp_path / "chain_ids.json"
        branch_ids_path = tmp_path / "branch_ids.json"
        run(str(dumps), str(chain_ids_path), str(branch_ids_path))

        branch_ids = json.loads(branch_ids_path.read_text(encoding="utf-8"))["ids"]
        assert set(branch_ids) == {f"{CHAIN_A}|27"}
