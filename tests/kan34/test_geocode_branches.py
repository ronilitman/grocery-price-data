"""scripts/geocode_branches.py (KAN-34): geocode every branch's own address.

Exercises the three-way classification, the resume mechanism (a branch
already in the state file is never re-queried) and the give-up-without-
misclassifying behaviour on a persistent error, against small synthetic
STORE_FILE dumps - not the real dumps or the real Nominatim endpoint, which
were verified separately (see the harvest commit and review/geocode_review.md).
"""

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import geocode_branches as gb  # noqa: E402

CHAIN_A = "7290000000001"


def store_file(chain_id, chain_name, stores):
    """stores: list of (store_id, name, address, city)."""
    store_xml = "".join(
        f"<Store><StoreID>{sid}</StoreID><StoreName>{name}</StoreName>"
        f"<Address>{addr}</Address><City>{city}</City></Store>"
        for sid, name, addr, city in stores
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


def setup_fixture(tmp_path, stores, branch_ids):
    """dumps dir + a branch_ids.json covering `stores`, loaded via monkeypatch."""
    dumps_dir = tmp_path / "dumps"
    write_dump(str(dumps_dir), "ChainA", "Stores1.xml",
               store_file(CHAIN_A, "Chain A", stores))
    return str(dumps_dir)


def patch_branch_uids(monkeypatch, mapping):
    monkeypatch.setattr(gb, "load_branch_uids", lambda: mapping)


class TestClassification:
    def test_geocoded_no_result_and_no_address(self, tmp_path, monkeypatch):
        stores = [
            ("1", "Has Address", "Herzl 1", "0"),
            ("2", "Blank Address", "unknown", "0"),
            ("3", "No Match", "Nowhere St 9", "0"),
        ]
        dumps_dir = setup_fixture(tmp_path, stores, {})
        patch_branch_uids(monkeypatch, {
            f"{CHAIN_A}|1": "uid-1", f"{CHAIN_A}|2": "uid-2", f"{CHAIN_A}|3": "uid-3",
        })

        def fake_geocode(query, timeout=15):
            if "Herzl" in query:
                return [{"lat": "32.0", "lon": "34.8"}]
            return []

        state_path = str(tmp_path / "state.json")
        records, state = gb.run(dumps_dir=dumps_dir, state_path=state_path,
                                 sleep_fn=lambda s: None, geocode_fn=fake_geocode)

        assert state["uid-1"]["status"] == "geocoded"
        assert state["uid-1"]["lat"] == 32.0
        assert state["uid-2"]["status"] == "no_address"
        assert state["uid-3"]["status"] == "no_result"


class TestResume:
    def test_an_already_resolved_branch_is_never_requeried(self, tmp_path, monkeypatch):
        stores = [("1", "Store", "Herzl 1", "0")]
        dumps_dir = setup_fixture(tmp_path, stores, {})
        patch_branch_uids(monkeypatch, {f"{CHAIN_A}|1": "uid-1"})
        state_path = str(tmp_path / "state.json")

        calls = {"n": 0}

        def fake_geocode(query, timeout=15):
            calls["n"] += 1
            return [{"lat": "32.0", "lon": "34.8"}]

        gb.run(dumps_dir=dumps_dir, state_path=state_path,
               sleep_fn=lambda s: None, geocode_fn=fake_geocode)
        assert calls["n"] == 1

        gb.run(dumps_dir=dumps_dir, state_path=state_path,
               sleep_fn=lambda s: None, geocode_fn=fake_geocode)
        assert calls["n"] == 1  # second run touched nothing new


class TestPersistentErrorLeavesBranchUnresolved:
    def test_giving_up_does_not_write_a_false_no_result(self, tmp_path, monkeypatch):
        stores = [("1", "Store", "Herzl 1", "0")]
        dumps_dir = setup_fixture(tmp_path, stores, {})
        patch_branch_uids(monkeypatch, {f"{CHAIN_A}|1": "uid-1"})
        state_path = str(tmp_path / "state.json")

        def always_fails(query, timeout=15):
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)

        records, state = gb.run(dumps_dir=dumps_dir, state_path=state_path,
                                 sleep_fn=lambda s: None, max_retries=2,
                                 geocode_fn=always_fails)
        assert "uid-1" not in state  # not misclassified as no_result

        locations = gb.write_locations(records, state, str(tmp_path / "out.json"))
        assert locations["uid-1"] is None  # still surfaces as unresolved, not a guess


class TestQueryBuilding:
    def test_query_includes_city_and_israel_when_city_known(self):
        record = {"address": "Herzl 1", "city_name": "תל אביב"}
        assert gb.build_query(record) == "Herzl 1, תל אביב, ישראל"

    def test_query_is_address_alone_when_no_city(self):
        record = {"address": "Herzl 1", "city_name": None}
        assert gb.build_query(record) == "Herzl 1"


class TestOutputs:
    def test_write_locations_and_review_cover_every_branch(self, tmp_path, monkeypatch):
        stores = [
            ("1", "A", "Herzl 1", "0"),
            ("2", "B", "unknown", "0"),
        ]
        dumps_dir = setup_fixture(tmp_path, stores, {})
        patch_branch_uids(monkeypatch, {f"{CHAIN_A}|1": "uid-1", f"{CHAIN_A}|2": "uid-2"})
        state_path = str(tmp_path / "state.json")

        def fake_geocode(query, timeout=15):
            return [{"lat": "32.0", "lon": "34.8"}]

        records, state = gb.run(dumps_dir=dumps_dir, state_path=state_path,
                                 sleep_fn=lambda s: None, geocode_fn=fake_geocode)
        out_path = str(tmp_path / "branch_locations.json")
        review_path = str(tmp_path / "review.md")
        locations = gb.write_locations(records, state, out_path)
        gb.write_review(records, state, review_path)

        assert set(locations) == {"uid-1", "uid-2"}
        assert locations["uid-1"]["lat"] == 32.0
        assert locations["uid-2"] is None

        with open(out_path, encoding="utf-8") as f:
            payload = json.load(f)
        assert "_meta" in payload
        assert payload["locations"]["uid-2"] is None

        review_text = open(review_path, encoding="utf-8").read()
        assert "Chain A" in review_text
