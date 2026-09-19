"""Deterministic address cleanup for the KAN-34 geocode second pass.

The first pass (scripts/geocode_branches.py) queried Nominatim with every
branch's raw Address + City and left 956 branches as "no result" - not
throttling or errors, just address strings Nominatim cannot parse: a leading
street-word prefix ("רח'"), a trailing descriptive clause after a comma
("... , ליד מכבי אש"), a city name already duplicated inside the address, or
plain formatting noise.

Every function here is a single named, meaning-preserving rule: it either
leaves an address alone or removes noise from it, never adding or guessing
anything. `clean_address` runs them in a fixed order and reports which ones
actually changed the string, so a caller can tell which rule gets credit for
a later hit. `street_plus_number` is the more aggressive third attempt
(street name + house number only, dropping any suffix) - not "cleaning" in
the same sense, but named and tested the same way.

No network calls in this module. See scripts/geocode_recover.py for the
Nominatim runner that uses it.
"""

import re

# Apostrophe-like characters seen in the real data standing in for a Hebrew
# geresh (רח', שד'): straight quote, geresh, curly quote, backtick. Gershayim
# (") is left alone - it is a different mark, used inside acronyms like
# בע"מ or ראשל"צ, not a street-prefix apostrophe.
_APOSTROPHE_VARIANTS = re.compile(r"[’׳`]")

_MULTI_WS = re.compile(r"\s+")

# A Hebrew letter directly followed by a digit with no space, e.g. a glued
# "הרצל22" - insert the gap so downstream regexes (prefix, house number) see
# a normal "street number" split. Digit-before-letter ("22א", a house-number
# letter suffix) is left alone; that gap is meaningful, not glued formatting.
_GLUED_NUMBER = re.compile(r"([א-ת])(\d)")

# Longest alternatives first so "רחוב"/"רח'" match before the bare "רח"
# fallback. "רח\"" is in the KAN-34 ticket's rule list though it does not
# appear in the current 956 (checked); kept for forward compatibility, it
# is harmless. Bare "שד" (no punctuation, 16 of the 956) was found in the
# real data during this pass and added alongside the listed "שד'"/"שדרות".
_STREET_PREFIX = re.compile(r'^(רחוב|שדרות|רח"|רח\'|שד\'|רח|שד)(?=\s)\s*')

_NUMBER = re.compile(r"\d+")

_HEBREW_LETTER = re.compile(r"[א-ת]")


def _collapse_ws(value):
    return _MULTI_WS.sub(" ", value).strip()


def _has_content(address):
    """A cleaned address must keep at least one Hebrew letter.

    A bare house/plot number with no street or place name can never be a
    meaningful standalone query - Nominatim will still return *some* point
    for "8, Beer Sheva", it just will not be the branch's address. Two real
    KAN-34 examples found this the hard way: a house-number-first address
    ("1 אורוגוואי") had street_plus_number keep everything up to the leading
    number, i.e. just the number; and "אלונים 1" (a moshav that uses its own
    name as the street) had drop_duplicate_city remove "אלונים" because it
    is also the city name, leaving just "1". Both are rejected by this
    guard rather than shipped as a coordinate for the wrong place.
    """
    return bool(_HEBREW_LETTER.search(address or ""))


def normalize_punctuation(address):
    """Canonical apostrophe, single spaces, and a gap before a glued number.

    >>> normalize_punctuation("שד` רוטשילד 14")
    "שד' רוטשילד 14"
    >>> normalize_punctuation("שד׳  רבין   16")
    "שד' רבין 16"
    """
    value = _APOSTROPHE_VARIANTS.sub("'", str(address or ""))
    value = _GLUED_NUMBER.sub(r"\1 \2", value)
    return _collapse_ws(value)


def strip_street_prefix(address):
    """Drop a leading street-word prefix: רח'/רח/רחוב/רח"/שד'/שדרות/שד.

    >>> strip_street_prefix("רח' גולדה מאיר 20")
    'גולדה מאיר 20'
    >>> strip_street_prefix("שד' רבין 16")
    'רבין 16'
    """
    return _STREET_PREFIX.sub("", address, count=1)


def drop_trailing_clause(address):
    """Drop everything after the first comma - a descriptive add-on.

    Covers both a genuine aside ("..., ליד מכבי אש") and a chain that has
    stuffed city/country into the Address field itself
    ("כישור 22, חולון, ישראל").

    >>> drop_trailing_clause("צהל 77 חדרה, ליד מכבי אש")
    'צהל 77 חדרה'
    >>> drop_trailing_clause("כישור 22, חולון, ישראל")
    'כישור 22'
    """
    return address.split(",", 1)[0].strip()


def drop_duplicate_city(address, city):
    """Remove the branch's own city name when it already appears in the address.

    Matches the city as a whole word (bounded by non-Hebrew-letter
    characters, or start/end of string) so it never eats part of an
    unrelated word.

    >>> drop_duplicate_city("צהל 77 חדרה", "חדרה")
    'צהל 77'
    >>> drop_duplicate_city("הרצל 1", "חדרה")
    'הרצל 1'
    """
    if not city:
        return address
    pattern = re.compile(r"(?<![א-ת])" + re.escape(city) + r"(?![א-ת])")
    return _collapse_ws(pattern.sub(" ", address))


# Order matters: normalize first so the prefix regex sees a canonical
# apostrophe, then strip the prefix, then drop a trailing clause, then a
# duplicated city - a city name is more likely to surface as its own token
# once the prefix and any comma-clause are already gone.
CLEANUP_RULES = [
    ("normalize_punctuation", lambda addr, city: normalize_punctuation(addr)),
    ("strip_street_prefix", lambda addr, city: strip_street_prefix(addr)),
    ("drop_trailing_clause", lambda addr, city: drop_trailing_clause(addr)),
    ("drop_duplicate_city", drop_duplicate_city),
]


def clean_address(address, city):
    """Run every rule in order. Returns (cleaned_address, rules_fired).

    A rule is "fired" only if it actually changed the string for this
    address - a no-op rule earns no credit. A rule whose result would strip
    the address down to no Hebrew letters at all (see `_has_content`) is
    rejected and skipped instead of applied - it destroyed the address
    rather than cleaning it.
    """
    value = str(address or "")
    fired = []
    for name, rule in CLEANUP_RULES:
        new_value = rule(value, city)
        if new_value != value and _has_content(new_value):
            fired.append(name)
            value = new_value
    return value, fired


def street_plus_number(address):
    """Street name + first house number, dropping anything after it.

    The more aggressive third attempt: unlike the cleanup rules above, this
    throws away trailing text unconditionally (a neighbourhood name, a mall
    name, a duplicated city with no comma and no exact-word match) rather
    than removing a specific known pattern. Returns None when the address
    has no digit at all - nothing to anchor on - or when taking everything
    up to that digit would leave no Hebrew letters (see `_has_content`): a
    house-number-first address like "1 אורוגוואי" would otherwise narrow to
    the bare number "1", losing the one piece of information ("אורוגוואי")
    that made the address findable.

    >>> street_plus_number('הע"ל 7 עמק שרה')
    'הע"ל 7'
    >>> street_plus_number("כביש 85 עכו צפת")
    'כביש 85'
    >>> street_plus_number("שדרות בן גוריון") is None
    True
    >>> street_plus_number("1 אורוגוואי") is None
    True
    """
    match = _NUMBER.search(address)
    if not match:
        return None
    candidate = _collapse_ws(address[: match.end()])
    if not _has_content(candidate):
        return None
    return candidate


def build_query(address, city):
    """Same shape as geocode_branches.build_query, taking address/city directly."""
    if city:
        return f"{address}, {city}, ישראל"
    return address


def progressive_attempts(address, city):
    """The ordered list of NEW queries worth trying for one branch.

    Stage order is original -> cleaned -> street+number, but the original
    is never included here - it is already known to miss (that's why the
    branch is in this pass at all). A stage is only included if it produces
    a query string different from every stage before it; an unchanged
    cleanup or a street+number identical to the cleaned address spends no
    extra Nominatim request.

    Returns a list of dicts: {"stage", "query", "rules"}.
    """
    original_query = build_query(address, city)
    seen = {original_query}
    attempts = []

    cleaned, rules = clean_address(address, city)
    cleaned_query = build_query(cleaned, city)
    if cleaned_query not in seen:
        attempts.append({"stage": "cleaned", "query": cleaned_query, "rules": rules})
        seen.add(cleaned_query)

    narrowed = street_plus_number(cleaned)
    if narrowed:
        narrowed_query = build_query(narrowed, city)
        if narrowed_query not in seen:
            attempts.append({
                "stage": "street_number",
                "query": narrowed_query,
                "rules": rules + ["street_plus_number"],
            })
            seen.add(narrowed_query)

    return attempts
