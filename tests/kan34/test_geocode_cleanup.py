"""scripts/geocode_cleanup.py (KAN-34 second pass): deterministic address
cleanup for the 956 branches scripts/geocode_branches.py left as "no result".

Each rule gets its own test against a real example pulled from
review/geocode_review.md's "No result" table (not a made-up fixture), plus a
test for the progressive attempt order: original (skipped, already known to
miss) -> cleaned -> street+number.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import geocode_cleanup as gc  # noqa: E402


class TestNormalizePunctuation:
    def test_backtick_apostrophe_is_canonicalized(self):
        # branch 7290492000005|... style addresses use a backtick for שד'
        assert gc.normalize_punctuation("שד` רוטשילד 14") == "שד' רוטשילד 14"

    def test_geresh_apostrophe_is_canonicalized(self):
        assert gc.normalize_punctuation("מתחם עמק סנטר עפולה שד׳ רבין 16") == \
            "מתחם עמק סנטר עפולה שד' רבין 16"

    def test_whitespace_is_collapsed(self):
        assert gc.normalize_punctuation("לחי 16  ,   ראשון לציון") == "לחי 16 , ראשון לציון"

    def test_glued_house_number_gets_a_gap(self):
        assert gc.normalize_punctuation("הרצל22") == "הרצל 22"

    def test_leaves_a_house_number_letter_suffix_alone(self):
        # digit-then-letter is a real house-number suffix, not glued formatting
        assert gc.normalize_punctuation("הרצל 22א") == "הרצל 22א"


class TestStripStreetPrefix:
    def test_reh_with_apostrophe(self):
        # branch_uid 001c1aa7... (Super-Pharm 302, Dimona) - the ticket's own example
        assert gc.strip_street_prefix("רח' גולדה מאיר 20") == "גולדה מאיר 20"

    def test_sderot_with_apostrophe(self):
        assert gc.strip_street_prefix("שד' רבין 16") == "רבין 16"

    def test_sderot_written_out(self):
        assert gc.strip_street_prefix("שדרות בן גוריון 5") == "בן גוריון 5"

    def test_bare_reh_no_punctuation(self):
        # 11 of the 956 use bare "רח" with no apostrophe at all
        assert gc.strip_street_prefix("רח יגאל אלון 22") == "יגאל אלון 22"

    def test_bare_sad_no_punctuation(self):
        # branch 023dac1f... (Tiberias) - "שד" with no punctuation, found in
        # the real 956 alongside the ticket's listed "שד'"/"שדרות" variants
        assert gc.strip_street_prefix("שד ספיר צומת 77 כניסה לטבריה ממערב") == \
            "ספיר צומת 77 כניסה לטבריה ממערב"

    def test_does_not_touch_an_address_with_no_prefix(self):
        assert gc.strip_street_prefix("חפץ חיים 4") == "חפץ חיים 4"


class TestDropTrailingClause:
    def test_drops_a_descriptive_aside(self):
        # branch 00a6eadd... (Dor Alon 968, Hadera)
        assert gc.drop_trailing_clause("צהל 77 חדרה, ליד מכבי אש") == "צהל 77 חדרה"

    def test_drops_a_chain_embedded_city_and_country(self):
        # branch f4825e17... (Hazi Hinam 202) - the raw Address field itself
        # already contains ", <city>, ישראל"
        assert gc.drop_trailing_clause("כישור 22, חולון, ישראל") == "כישור 22"

    def test_leaves_a_comma_free_address_alone(self):
        assert gc.drop_trailing_clause("גולדה מאיר 20") == "גולדה מאיר 20"


class TestDropDuplicateCity:
    def test_removes_the_city_when_glued_with_no_comma(self):
        # after strip_street_prefix + drop_trailing_clause, "צהל 77 חדרה"
        # still carries its own city with no comma to catch it
        assert gc.drop_duplicate_city("צהל 77 חדרה", "חדרה") == "צהל 77"

    def test_does_not_touch_an_unrelated_word_containing_the_city_as_substring(self):
        # "חדרהוראה" is not a real street name, but proves word-boundary safety
        assert gc.drop_duplicate_city("חדרהוראה 1", "חדרה") == "חדרהוראה 1"

    def test_no_city_known_is_a_no_op(self):
        assert gc.drop_duplicate_city("הרצל 1", None) == "הרצל 1"


class TestCleanAddress:
    def test_reports_every_rule_that_actually_fired(self):
        cleaned, rules = gc.clean_address("שד` צהל 77 חדרה, ליד מכבי אש", "חדרה")
        assert cleaned == "צהל 77"
        assert rules == ["normalize_punctuation", "strip_street_prefix",
                          "drop_trailing_clause", "drop_duplicate_city"]

    def test_a_rule_that_does_not_change_anything_is_not_reported(self):
        cleaned, rules = gc.clean_address("חפץ חיים 4", "נתניה")
        assert cleaned == "חפץ חיים 4"
        assert rules == []


class TestStreetPlusNumber:
    def test_drops_a_neighbourhood_suffix_with_no_comma_and_no_duplicate_city(self):
        # branch 005792b3... (Rami Levy 27, Be'er Sheva) - "עמק שרה" is a
        # neighbourhood, not the city ("באר שבע"), so drop_duplicate_city
        # cannot touch it; only the more aggressive street+number attempt can
        assert gc.street_plus_number('הע"ל 7 עמק שרה') == 'הע"ל 7'

    def test_keeps_a_highway_number_as_the_anchor_digit(self):
        assert gc.street_plus_number("כביש 85 עכו צפת") == "כביש 85"

    def test_none_when_there_is_no_digit_at_all(self):
        assert gc.street_plus_number("שדרות בן גוריון") is None


class TestProgressiveAttempts:
    def test_order_is_cleaned_then_street_number_never_the_original(self):
        address, city = 'הע"ל 7 עמק שרה', "באר שבע"
        attempts = gc.progressive_attempts(address, city)
        stages = [a["stage"] for a in attempts]
        assert stages == ["street_number"]  # "cleaned" is a no-op here, so skipped
        assert attempts[0]["query"] == 'הע"ל 7, באר שבע, ישראל'
        original_query = gc.build_query(address, city)
        assert attempts[0]["query"] != original_query

    def test_both_stages_present_and_in_order_when_both_change_the_query(self):
        # branch e65523b7... (Hazi Hinam 205) - "ראשל\"צ" (an abbreviation
        # for the city) is left over after dropping the trailing clause, so
        # "cleaned" and "street+number" are two genuinely different queries
        address, city = 'הלחי 16 ראשל"צ , ישראל', "ראשון לציון"
        attempts = gc.progressive_attempts(address, city)
        stages = [a["stage"] for a in attempts]
        assert stages == ["cleaned", "street_number"]
        assert attempts[0]["query"] == 'הלחי 16 ראשל"צ, ראשון לציון, ישראל'
        assert attempts[1]["query"] == "הלחי 16, ראשון לציון, ישראל"
        assert attempts[0]["query"] != attempts[1]["query"]

    def test_no_attempts_when_cleaning_changes_nothing_and_there_is_no_digit(self):
        # branch 0346649e... (Tel Sheva) - "לתל שבע" glues the city onto the
        # previous word with no space, so drop_duplicate_city correctly
        # leaves it alone (word-boundary safety), there is no prefix, no
        # comma and no digit: nothing our rules can do, no new query to try
        assert gc.progressive_attempts("בכניסה לתל שבע", "תל שבע") == []

    def test_street_number_is_skipped_when_identical_to_the_cleaned_query(self):
        # cleaning already narrows to street+number with nothing left over,
        # so the third stage would just repeat the second request
        address, city = "רח' גולדה מאיר 20", "דימונה"
        attempts = gc.progressive_attempts(address, city)
        assert [a["stage"] for a in attempts] == ["cleaned"]
