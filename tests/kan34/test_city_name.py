"""build_chain_db.resolve_city_name(): the three shapes STORE_FILE sends.

Nineteen of the 22 chains checked for KAN-34 send the CBS (Lamas) locality
code as ``City`` - a number, meaningless without data/city_codes.json. Three
send a Hebrew name outright. Four send nothing at all: empty, ``'0'``, or (one
chain's placeholder) the literal string ``'unknown'``. All three shapes have
to resolve to the right thing, and a numeric code with no entry in the CBS
table has to come back None rather than a guess - the owner reviews nulls,
not invented names.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import build_chain_db  # noqa: E402

CODES = {
    "2530": "באר יעקב",
    "3000": "ירושלים",
    "5000": "תל אביב -יפו",
}


class TestNumericCode:
    def test_a_known_code_resolves_to_its_hebrew_name(self):
        assert build_chain_db.resolve_city_name("3000", CODES) == "ירושלים"

    def test_an_unknown_numeric_code_is_null_not_a_guess(self):
        assert build_chain_db.resolve_city_name("9999999", CODES) is None


class TestHebrewName:
    def test_a_hebrew_name_is_used_as_is(self):
        assert build_chain_db.resolve_city_name("גן יבנה", CODES) == "גן יבנה"

    def test_whitespace_is_normalised(self):
        assert build_chain_db.resolve_city_name("תל  אביב   יפו", CODES) == "תל אביב יפו"


class TestEmpty:
    def test_an_empty_string_is_null(self):
        assert build_chain_db.resolve_city_name("", CODES) is None

    def test_the_zero_placeholder_is_null(self):
        assert build_chain_db.resolve_city_name("0", CODES) is None

    def test_the_literal_unknown_placeholder_is_null(self):
        # City Market's placeholder for a branch it has no city for.
        assert build_chain_db.resolve_city_name("unknown", CODES) is None

    def test_none_input_is_null(self):
        assert build_chain_db.resolve_city_name(None, CODES) is None
