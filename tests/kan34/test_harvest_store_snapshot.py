"""harvest_store_snapshot.load_snapshot(): parses the snapshot shape, and
the committed file (once harvested) has every field KAN-34's later matching
step will need.

data/cheapersal_stores.json is a manual one-off snapshot of a third-party
API (cheapersal/additlist), harvested by scripts/harvest_store_snapshot.py.
As of this commit the API host is refusing all connections (verified from
several networks - see the harvest commit message), so no snapshot has been
harvested yet and the file does not exist. The real-file test below skips
itself rather than failing until that snapshot is committed; the fixture
test exercises the same loader against a small synthetic file in the
meantime, so the parsing logic itself is still covered.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import harvest_store_snapshot  # noqa: E402

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "cheapersal_stores.json"
)

REQUIRED_FIELDS = ("_id", "location", "chainName", "city", "address", "name")

FIXTURE_DOC = {
    "_meta": {"description": "fixture", "endpoint": "https://example.invalid/",
               "harvested": "2026-01-01", "store_count": 1},
    "stores": {
        "abc123": {
            "_id": "abc123",
            "location": [34.78, 32.08],
            "phone": "03-0000000",
            "city": "תל אביב - יפו",
            "address": "רחוב כלשהו 1",
            "distance": 12.3,
            "name": "חנות לדוגמה",
            "chainName": "TEST CHAIN",
            "logo": "https://example.invalid/logo.png",
            "isOnline": False,
            "supermarketId": "xyz789",
        }
    },
}


def _assert_valid_record(store_id, record):
    for field in REQUIRED_FIELDS:
        assert field in record, f"{store_id} is missing {field!r}"
    assert record["_id"] == store_id

    location = record["location"]
    assert isinstance(location, list)
    assert len(location) == 2
    lon, lat = location
    assert isinstance(lon, float)
    assert isinstance(lat, float)

    # Israel's longitude (~34-36) and latitude (~29-33) ranges don't
    # overlap, so checking both confirms the order is [lon, lat], not
    # [lat, lon].
    assert 33 < lon < 37, f"{store_id}: lon {lon} outside Israel's range"
    assert 29 < lat < 34, f"{store_id}: lat {lat} outside Israel's range"


def test_the_loader_parses_the_snapshot_shape(tmp_path):
    fixture_path = tmp_path / "cheapersal_stores.json"
    fixture_path.write_text(json.dumps(FIXTURE_DOC), encoding="utf-8")

    stores = harvest_store_snapshot.load_snapshot(str(fixture_path))

    assert isinstance(stores, dict)
    assert len(stores) == 1
    _assert_valid_record("abc123", stores["abc123"])


@pytest.mark.skipif(
    not os.path.exists(SNAPSHOT_PATH),
    reason="data/cheapersal_stores.json not harvested yet - "
           "the cheapersal API was down when KAN-34 step 3 was attempted",
)
def test_the_committed_snapshot_loads_and_every_record_is_valid():
    stores = harvest_store_snapshot.load_snapshot(SNAPSHOT_PATH)
    assert isinstance(stores, dict)
    assert len(stores) > 0
    for store_id, record in stores.items():
        _assert_valid_record(store_id, record)
