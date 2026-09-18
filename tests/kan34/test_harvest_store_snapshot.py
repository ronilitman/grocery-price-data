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

import io
import json
import os
import sys
import urllib.error

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


# --- harvest() resume and ban handling (KAN-34: survive a ban and resume) ---
#
# These mock probe() entirely - no network access - and use a tiny
# two-location universe so each test only has to reason about a couple of
# probes. One barcode is used throughout - see
# test_one_probe_per_location_using_a_single_barcode for that dimension.

LOCATIONS_2 = {"CityA": (34.0, 32.0), "CityB": (35.0, 31.0)}
BARCODES_1 = {"item": "111"}


def _noop_sleep(seconds):
    pass


def test_one_probe_per_location_using_a_single_barcode(tmp_path, monkeypatch):
    """The barcode dimension was measured to be useless in production (see
    the module docstring): probing costs must scale with locations only."""
    state_path = str(tmp_path / "state.json")
    locations = {"CityA": (34.0, 32.0), "CityB": (35.0, 31.0), "CityC": (34.5, 32.5)}
    calls = []

    def fake_probe(barcode, lon, lat):
        calls.append((barcode, lon, lat))
        return []

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    harvest_store_snapshot.harvest(
        barcodes=harvest_store_snapshot.BARCODES, locations=locations,
        patience=10, delay=0, state_path=state_path, sleep_fn=_noop_sleep,
    )

    assert len(calls) == len(locations)  # one probe per location, not per (location, barcode)
    barcodes_used = {c[0] for c in calls}
    assert barcodes_used == set(harvest_store_snapshot.BARCODES.values())
    assert len(harvest_store_snapshot.BARCODES) == 1  # the default is one barcode


def test_resume_skips_completed_pairs_and_keeps_collected_stores(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    harvest_store_snapshot.save_state(
        {
            "stores": {"s1": {"_id": "s1", "chainName": "OLD"}},
            "done_pairs": [harvest_store_snapshot._pair_key("CityA")],
            "zero_streak": 0,
            "log": [],
        },
        state_path,
    )

    calls = []

    def fake_probe(barcode, lon, lat):
        calls.append((barcode, lon, lat))
        # CityA is already done; only CityB should ever be probed.
        assert (lon, lat) == LOCATIONS_2["CityB"]
        return [{"_id": "s2", "chainName": "NEW"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes=BARCODES_1, locations=LOCATIONS_2, patience=10,
        delay=0, state_path=state_path, sleep_fn=_noop_sleep,
    )

    assert len(calls) == 1  # CityA's probe was skipped, not repeated
    assert gave_up is False
    assert set(stores) == {"s1", "s2"}  # old store kept, new one added

    saved = harvest_store_snapshot.load_state(state_path)
    assert set(saved["done_pairs"]) == {
        harvest_store_snapshot._pair_key("CityA"),
        harvest_store_snapshot._pair_key("CityB"),
    }


def test_resume_migrates_an_old_city_plus_barcode_state_file(tmp_path, monkeypatch):
    """Before this change done_pairs was keyed "city\\x1ebarcode label"
    (one entry per barcode tried at a city). Production's real state file
    has exactly this shape for Tel Aviv: 5 barcode-labelled entries and 465
    already-collected stores that must not be re-fetched or discarded."""
    state_path = str(tmp_path / "state.json")
    harvest_store_snapshot.save_state(
        {
            "stores": {f"s{i}": {"_id": f"s{i}", "chainName": "OLD"} for i in range(465)},
            "done_pairs": [
                "Tel Aviv\x1emilk (Tnuva 3% carton)",
                "Tel Aviv\x1eBamba (Osem 80g)",
                "Tel Aviv\x1ebread (Angel sliced, 750g)",
                "Tel Aviv\x1eeggs (Tnuva L, 12ct)",
                "Tel Aviv\x1ecleaning (Sano Spark dish soap, 1L)",
            ],
            "zero_streak": 4,
            "log": [],
        },
        state_path,
    )

    calls = []

    def fake_probe(barcode, lon, lat):
        calls.append((lon, lat))
        return [{"_id": "s_new", "chainName": "NEW"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes=BARCODES_1, locations={"Tel Aviv": (34.7818, 32.0853), "Haifa": (34.9896, 32.7940)},
        patience=10, delay=0, state_path=state_path, sleep_fn=_noop_sleep,
    )

    assert calls == [(34.9896, 32.7940)]  # Tel Aviv skipped (migrated as done), only Haifa probed
    assert len(stores) == 466  # the 465 old stores, kept, plus Haifa's new one
    assert all(f"s{i}" in stores for i in range(465))

    saved = harvest_store_snapshot.load_state(state_path)
    assert set(saved["done_pairs"]) == {"Tel Aviv", "Haifa"}


def test_connection_error_waits_and_resumes_instead_of_crashing(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    attempts = {"n": 0}
    sleep_calls = []

    def fake_probe(barcode, lon, lat):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionRefusedError("[Errno 61] Connection refused")
        return [{"_id": "s1", "chainName": "X"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s), ban_wait=2700,
    )

    assert attempts["n"] == 2  # failed once, then the same pair was retried
    assert sleep_calls[0] == 2700  # waited out the ban, no tight retry
    assert gave_up is False
    assert "s1" in stores
    assert any(not e.get("ok", True) for e in log)  # the failure is in the log


def test_gives_up_after_max_consecutive_ban_cycles_with_no_progress(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    sleep_calls = []

    def always_banned(barcode, lon, lat):
        raise TimeoutError("timed out")

    monkeypatch.setattr(harvest_store_snapshot, "probe", always_banned)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s), ban_wait=1, max_ban_cycles=3,
    )

    assert gave_up is True
    # 3 bans, but no sleep before giving up on the last one - it stops there.
    assert len(sleep_calls) == 3 - 1
    assert stores == {}


def test_state_is_saved_after_each_successful_probe_not_only_at_the_end(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    seen_on_disk_before_second_probe = {}

    def fake_probe(barcode, lon, lat):
        if (lon, lat) == LOCATIONS_2["CityB"]:
            # By the time CityB is probed, CityA's probe must already be
            # persisted to disk - not just held in memory.
            on_disk = harvest_store_snapshot.load_state(state_path)
            seen_on_disk_before_second_probe.update(on_disk["stores"])
            return [{"_id": "s2", "chainName": "B"}]
        return [{"_id": "s1", "chainName": "A"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    harvest_store_snapshot.harvest(
        barcodes=BARCODES_1, locations=LOCATIONS_2, patience=10,
        delay=0, state_path=state_path, sleep_fn=_noop_sleep,
    )

    assert "s1" in seen_on_disk_before_second_probe


# --- HTTP status handling: a 404 is a normal answer, not a ban; 429/5xx is
# a real backoff but still not the IP ban; other 4xx is recorded and
# skipped; only a connection failure is treated as the ban. ---

def _http_error(code, msg="error"):
    return urllib.error.HTTPError("http://x.invalid", code, msg, {}, io.BytesIO(b""))


def test_404_advances_without_sleeping_or_retrying(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    calls = {"n": 0}
    sleep_calls = []

    def fake_probe(barcode, lon, lat):
        calls["n"] += 1
        raise _http_error(404, "Not Found")

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s),
    )

    assert calls["n"] == 1  # not retried
    assert sleep_calls == [0]  # only the normal --delay(=0), never the ban wait
    assert gave_up is False
    assert stores == {}
    assert log[0]["ok"] is True
    assert log[0]["new"] == 0

    saved = harvest_store_snapshot.load_state(state_path)
    assert harvest_store_snapshot._pair_key("CityA") in saved["done_pairs"]


def test_429_sleeps_the_ban_wait_and_retries_the_same_pair(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    attempts = {"n": 0}
    sleep_calls = []

    def fake_probe(barcode, lon, lat):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, "Too Many Requests")
        return [{"_id": "s1", "chainName": "X"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s), ban_wait=1800,
    )

    assert attempts["n"] == 2  # retried the same pair after backing off
    assert sleep_calls[0] == 1800  # the ban wait, not the ordinary delay
    assert gave_up is False
    assert "s1" in stores


def test_connection_refusal_still_takes_the_ban_path(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    attempts = {"n": 0}
    sleep_calls = []

    def fake_probe(barcode, lon, lat):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionRefusedError("[Errno 61] Connection refused")
        return [{"_id": "s1", "chainName": "X"}]

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s), ban_wait=2700,
    )

    assert attempts["n"] == 2
    assert sleep_calls[0] == 2700
    assert gave_up is False
    assert "s1" in stores


def test_other_4xx_is_recorded_and_skipped_without_retry_or_sleep(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.json")
    calls = {"n": 0}
    sleep_calls = []

    def fake_probe(barcode, lon, lat):
        calls["n"] += 1
        raise _http_error(400, "Bad Request")

    monkeypatch.setattr(harvest_store_snapshot, "probe", fake_probe)

    stores, log, stopped_early, gave_up = harvest_store_snapshot.harvest(
        barcodes={"item": "111"}, locations={"CityA": (34.0, 32.0)},
        patience=10, delay=0, state_path=state_path,
        sleep_fn=lambda s: sleep_calls.append(s),
    )

    assert calls["n"] == 1  # not retried
    assert sleep_calls == []  # no delay, no ban wait
    assert gave_up is False
    assert stores == {}
    assert log[0]["ok"] is False

    saved = harvest_store_snapshot.load_state(state_path)
    assert harvest_store_snapshot._pair_key("CityA") in saved["done_pairs"]
