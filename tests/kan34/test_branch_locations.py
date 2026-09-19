"""scripts/branch_locations.load_branch_locations(): the loader side of
data/branch_locations.json (KAN-34). A branch that never resolved is `null`
in the file, and a branch never geocoded at all is simply absent - neither
should raise or need special-casing downstream.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import branch_locations  # noqa: E402


def write_locations_file(path, locations):
    payload = {"_meta": {"source": "test"}, "locations": locations}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


class TestReading:
    def test_reads_a_geocoded_entry(self, tmp_path):
        path = tmp_path / "branch_locations.json"
        write_locations_file(str(path), {
            "uid-1": {"lat": 32.05, "lon": 34.77, "city": "תל אביב", "method": "nominatim"},
        })
        result = branch_locations.load_branch_locations(str(path))
        assert result["uid-1"] == {"lat": 32.05, "lon": 34.77, "city": "תל אביב",
                                    "method": "nominatim"}


class TestMissingBranch:
    def test_a_branch_never_in_the_file_has_no_coordinates(self, tmp_path):
        path = tmp_path / "branch_locations.json"
        write_locations_file(str(path), {"uid-1": {"lat": 1.0, "lon": 2.0}})
        result = branch_locations.load_branch_locations(str(path))
        assert result.get("uid-does-not-exist") is None


class TestNullBreaksNothing:
    def test_a_null_entry_reads_back_as_none(self, tmp_path):
        path = tmp_path / "branch_locations.json"
        write_locations_file(str(path), {"uid-1": None})
        result = branch_locations.load_branch_locations(str(path))
        assert result["uid-1"] is None
        # a caller doing `.get(uid)` and checking for None sees the same
        # thing whether the key is absent or explicitly null
        assert result.get("uid-1") == result.get("uid-missing")


class TestMissingFile:
    def test_no_file_yet_returns_empty_dict_not_an_error(self, tmp_path):
        path = tmp_path / "does_not_exist.json"
        assert branch_locations.load_branch_locations(str(path)) == {}
