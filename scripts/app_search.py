"""One normaliser, shared by index time (build_app_db.py, KAN-8) and query
time (the API's ``/search``, KAN-12), so a token that survives filler
stripping when a name is indexed is tokenised exactly the same way when a
user types it.

``normalise``/``tokens`` mirror two existing rules rather than inventing a
third: the quote characters stripped here are ``build_catalog.NAME_QUOTES``'s
four (ASCII double quote, ASCII apostrophe, Hebrew gershayim U+05F4, Hebrew
geresh U+05F3) so ``ק"ג`` and ``קג`` still collide, and the word split is
``merge_db.TOKEN_SPLIT`` - anything that is neither a word character nor in
the Hebrew block U+0590-U+05FF - so a barcode-ish ``15%`` splits into ``15``
the same way in both places.
"""

import re
from collections import Counter

QUOTES = re.compile(r"[\"'״׳]")
WHITESPACE = re.compile(r"\s+")
TOKEN_SPLIT = re.compile(r"[^\w֐-׿]+", re.UNICODE)

# Same rule build_catalog.NAME_FILLER_AT applies: a word carried by 1% of
# products, or at least 50 of them, describes packaging rather than product
# and would drown every query that includes it (see the FTS worst case on
# ``גרם`` in the KAN-8 spec).
FILLER_AT = 0.01
FILLER_FLOOR = 50


def normalise(text):
    """Lower-case, strip the four quote characters, collapse whitespace."""
    text = QUOTES.sub("", text or "")
    return WHITESPACE.sub(" ", text.lower()).strip()


def tokens(text):
    """Normalised text split into indexable tokens (2+ characters)."""
    return [t for t in TOKEN_SPLIT.split(normalise(text)) if len(t) >= 2]


def compute_filler(names, filler_at=FILLER_AT, floor=FILLER_FLOOR):
    """Tokens appearing on at least ``filler_at`` of ``names`` (min ``floor``).

    ``names`` may be iterated once; every name is only tokenised once
    regardless of how many times it repeats a token (a set per name), same as
    ``build_catalog.write_names`` counting document frequency, not raw counts.
    """
    freq = Counter()
    total = 0
    for name in names:
        total += 1
        freq.update(set(tokens(name)))
    if total == 0:
        return set()
    threshold = max(floor, total * filler_at)
    return {token for token, count in freq.items() if count >= threshold}


def index_text(name, filler):
    """A product's name, normalised and with filler tokens dropped.

    This is what goes into ``fts_all``/``fts_deals``' indexed ``name``
    column - never the raw name - so a filler word can't sneak back in via a
    substring match FTS5's tokenizer would still honour.
    """
    return " ".join(t for t in tokens(name) if t not in filler)


def build_match(query, filler):
    """An FTS5 ``MATCH`` expression with OR semantics, or ``None``.

    Every token is double-quoted (doubling an embedded ``"``) because a raw
    token handed to ``MATCH`` is a syntax error waiting to happen - ``15%``
    raises ``fts5: syntax error near "%"`` unquoted. Filler tokens are
    dropped the same way they were at index time, and an all-filler (or
    entirely unindexable) query returns ``None`` so the caller can skip
    running a query at all rather than asking FTS5 to match nothing.
    """
    kept = [t for t in tokens(query) if t not in filler]
    if not kept:
        return None
    quoted = ['"{}"'.format(t.replace('"', '""')) for t in kept]
    return " OR ".join(quoted)
