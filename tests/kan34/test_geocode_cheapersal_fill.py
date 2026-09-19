"""scripts/geocode_cheapersal_fill.py (KAN-34 third pass): exact-address fill
from the third-party cheapersal store list, plus the post-fill sanity check.

Each normalisation step gets its own test against a real-shaped example, and
compute_fill/sanity_check are exercised against small synthetic inputs - not
the real 2,324 branches or cheapersal's 1,945 stores, which were run and
reported in review/geocode_review.md.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import geocode_cheapersal_fill as gcf  # noqa: E402


class TestNormalizeAddress:
    def test_strips_trailing_city_suffix(self):
        assert gcf.normalize_address("סוקולוב 10 , הרצליה", "הרצליה") == "סוקולוב 10"

    def test_strips_leading_reh_apostrophe(self):
        assert gcf.normalize_address("רח' יוסי שריד 6") == "יוסי שריד 6"

    def test_strips_leading_reh_geresh_quote(self):
        assert gcf.normalize_address('רח"א 1') == 'א 1'

    def test_strips_leading_rehov(self):
        assert gcf.normalize_address("רחוב הרצל 5") == "הרצל 5"

    def test_strips_leading_sderot_abbrev(self):
        assert gcf.normalize_address("שד' רבין 16") == "רבין 16"

    def test_strips_leading_sderot_full(self):
        assert gcf.normalize_address("שדרות הרימון 1") == "הרימון 1"

    def test_does_not_touch_a_street_that_merely_starts_with_reh(self):
        # "רחל" (Rachel) is not the רח'/רח"/רחוב prefix - must survive intact.
        assert gcf.normalize_address("רחל אמנו 3") == "רחל אמנו 3"

    def test_normalizes_street_number_gap_letter_then_digit(self):
        assert gcf.normalize_address("אבן גבירול157") == "אבן גבירול 157"

    def test_collapses_glued_gap_both_ways(self):
        assert gcf.normalize_address("157אבן גבירול") == "157 אבן גבירול"

    def test_collapses_whitespace(self):
        assert gcf.normalize_address("הרצל   5") == "הרצל 5"

    def test_drops_trailing_comma(self):
        assert gcf.normalize_address("הרצל 5,") == "הרצל 5"

    def test_drops_trailing_period(self):
        assert gcf.normalize_address("הרצל 5.") == "הרצל 5"

    def test_removes_gershayim_in_abbreviation(self):
        # cheapersal's own copy of the same address drops the mark too - see
        # the real match this enables in review/geocode_review.md.
        assert gcf.normalize_address('אח"י אילת 34') == "אחי אילת 34"


class TestComputeFill:
    def _store(self, sid, address, city, lat, lon):
        return sid, {"_id": sid, "address": address, "city": city, "location": [lon, lat]}

    def test_same_city_is_accepted(self):
        locations = {"u1": None}
        our = {"u1": {"branch_uid": "u1", "address": "הרצל 5", "city": "חולון"}}
        sid, store = self._store("s1", "הרצל 5 , חולון", "חולון", 32.0, 34.7)
        index = gcf.build_cheapersal_index({sid: store})
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert list(accepted) == ["u1"]
        assert accepted["u1"]["city"] == "חולון"
        assert not review and not ambiguous

    def test_blank_or_differing_city_goes_to_review_not_accepted(self):
        locations = {"u1": None}
        our = {"u1": {"branch_uid": "u1", "address": "הרצל 5", "city": "תל אביב"}}
        sid, store = self._store("s1", "הרצל 5 , חולון", "חולון", 32.0, 34.7)
        index = gcf.build_cheapersal_index({sid: store})
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert not accepted
        assert list(review) == ["u1"]

    def test_one_to_one_guard_two_of_ours_same_address(self):
        locations = {"u1": None, "u2": None}
        our = {
            "u1": {"branch_uid": "u1", "address": "הרצל 5", "city": "חולון"},
            "u2": {"branch_uid": "u2", "address": "הרצל 5", "city": "חולון"},
        }
        sid, store = self._store("s1", "הרצל 5 , חולון", "חולון", 32.0, 34.7)
        index = gcf.build_cheapersal_index({sid: store})
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert not accepted and not review
        assert len(ambiguous) == 1

    def test_one_to_one_guard_two_of_theirs_same_address(self):
        locations = {"u1": None}
        our = {"u1": {"branch_uid": "u1", "address": "הרצל 5", "city": "חולון"}}
        s1 = self._store("s1", "הרצל 5 , חולון", "חולון", 32.0, 34.7)
        s2 = self._store("s2", "הרצל 5 , חולון", "חולון", 32.1, 34.8)
        index = gcf.build_cheapersal_index(dict([s1, s2]))
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert not accepted
        assert len(ambiguous) == 1

    def test_already_resolved_branch_is_never_touched(self):
        locations = {"u1": {"lat": 1, "lon": 2, "city": "x", "method": "nominatim"}}
        our = {"u1": {"branch_uid": "u1", "address": "הרצל 5", "city": "חולון"}}
        sid, store = self._store("s1", "הרצל 5 , חולון", "חולון", 32.0, 34.7)
        index = gcf.build_cheapersal_index({sid: store})
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert not accepted and not review and not ambiguous

    def test_unknown_address_is_never_matched(self):
        locations = {"u1": None}
        our = {"u1": {"branch_uid": "u1", "address": "unknown", "city": None}}
        sid, store = self._store("s1", "unknown", "חולון", 32.0, 34.7)
        index = gcf.build_cheapersal_index({sid: store})
        accepted, review, ambiguous = gcf.compute_fill(locations, our, index)
        assert not accepted and not review and not ambiguous


class TestSanityCheck:
    def _stores(self, city, points):
        return {
            f"s{i}": {"address": "x", "city": city, "location": [lon, lat]}
            for i, (lat, lon) in enumerate(points)
        }

    def test_within_radius_is_not_flagged(self):
        locations = {"u1": {"lat": 32.001, "lon": 34.701, "city": "חולון", "method": "nominatim"}}
        stores = self._stores("חולון", [(32.0, 34.7)])
        assert gcf.sanity_check(locations, stores) == []

    def test_far_from_every_known_point_in_its_city_is_flagged(self):
        locations = {"u1": {"lat": 33.5, "lon": 35.5, "city": "חולון", "method": "nominatim"}}
        stores = self._stores("חולון", [(32.0, 34.7)])
        flagged = gcf.sanity_check(locations, stores)
        assert len(flagged) == 1
        assert flagged[0]["reason"] == "far_from_city"
        assert flagged[0]["distance_km"] > gcf.SANITY_RADIUS_KM

    def test_outside_israel_bbox_is_flagged_even_with_no_city_reference(self):
        locations = {"u1": {"lat": 48.85, "lon": 2.35, "city": "פריז", "method": "nominatim"}}
        assert gcf.sanity_check(locations, {})[0]["reason"] == "outside_israel_bbox"

    def test_null_entries_are_skipped(self):
        locations = {"u1": None}
        assert gcf.sanity_check(locations, {}) == []

    def test_city_with_no_reference_points_is_not_flagged(self):
        # nothing to check against - not evidence of a problem.
        locations = {"u1": {"lat": 32.0, "lon": 34.7, "city": "עיר שלא קיימת", "method": "nominatim"}}
        assert gcf.sanity_check(locations, {}) == []
