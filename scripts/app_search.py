"""One normaliser, shared by index time (build_app_db.py, KAN-8) and query
time (the API's ``/search``, KAN-12), so a query is tokenised exactly the
same way a name was when it was indexed.

**Filler is a query-time concept only** (reviewer override on the original
KAN-8 spec, 2026-09-14). The first cut of this module also stripped filler
tokens from the indexed text; on real data that turned out to delete real
product words, not just packaging noise - ``שוקולד`` (chocolate), ``עוף``
(chicken), ``יין`` (wine), ``עוגיות`` (cookies) and ``סוכריות`` (sweets) all
cleared the 1% threshold and vanished from the index, so searching for any
of them found nothing. FTS5's ``bm25()`` already ranks a document dominated
by a common word below one where the query term is distinctive, which is
what filler was trying to approximate by hand - so now every token is
indexed, and queries keep every token too (see ``build_match``). The
``fts_filler`` table is still built; nothing uses it to drop words today.

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


def index_text(name):
    """A product's name, normalised into the tokens ``fts_all``/``fts_deals``
    index - every token, filler included (see the module docstring for why).
    """
    return " ".join(tokens(name))


def build_match(query, filler=None):
    """An FTS5 ``MATCH`` expression with OR semantics, or ``None``.

    Every token is double-quoted (doubling an embedded ``"``) because a raw
    token handed to ``MATCH`` is a syntax error waiting to happen - ``15%``
    raises ``fts5: syntax error near "%"`` unquoted.

    Every token is kept, filler included. Dropping filler even when other
    words survive made rankings worse on real data: ``"שוקולד חלב"``
    (chocolate milk) searched only ``חלב`` and put plain milk first, while
    searching both words puts chocolate milk first. bm25 already weights a
    common word low, and on the real catalogue the widest query stays under
    ~20 ms. ``filler`` is accepted so callers don't change, and ignored.
    ``None`` means the query tokenised to nothing at all.
    """
    toks = tokens(query)
    if not toks:
        return None
    quoted = ['"{}"'.format(t.replace('"', '""')) for t in toks]
    return " OR ".join(quoted)
