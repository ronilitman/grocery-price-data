"""Loose produce, grouped across chains into one sellable thing.

Every chain gives its own barcode to a kilo of tomatoes - 15 of them for
`עגבניה` alone, and one of those codes means frozen chicken breast at a
different chain. So a barcode identifies a row in a chain's price file, not a
product. Anyone asking "what do tomatoes cost" needs the 15 collapsed into one.

Scope is deliberately narrow: fruit and vegetables sold by weight. That is
where the evidence is - short names, a real per-kilo price, a small closed
vocabulary you can read in a minute - and it is most of what goes on a list.
The deli counter (`פילה אמנון קפוא 5-7`, `בולגרית 24%`) groups far worse and
is left alone rather than guessed at.

Four rules decide whether two names are the same product:

  1. Numbers must match exactly. `בשר מס 2` is not `בשר מס 8`, `בולגרית 5%` is
     not `24%`. Any difference and the answer is no.
  2. Filler is dropped - a word on 1%+ of all 243k products describes how a
     thing is sold, not what it is. `טרי` is on 4,441 of them; `עגבניה` on 51.
     PROTECTED holds the handful common enough to look like filler that
     actually tell products apart, `לבן`/`אדום` above all.
  3. Two words are the same word when they share a 60% prefix AND the leftover
     is a Hebrew plural ending. The plural test is what stops `חלב` matching
     `חלבה` at 0.75 similarity.
  4. Score = matched words over the LONGER word count. Dividing by the longer
     count is what makes an extra word count against you, so `עגבניה` and
     `עגבניה שרי` score 0.5 and stay apart while `עגבניה`/`עגבניות` score 1.0.
"""

import json
import os
import re
from collections import Counter, defaultdict

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

WORD = re.compile(r"[\w\"'״׳%]+", re.UNICODE)
# Hebrew is written with and without the extra yod/vav, by different chains for
# the same thing: Shufersal's `עגבנייה` is everyone else's `עגבניה`, and it was
# forming a group of its own holding the cheapest tomato in the country.
KTIV = ((r"יי", "י"), (r"וו", "ו"))
# A bare number, a percentage, or a size range - all of them identify.
SPEC = re.compile(r"^[\d.]+(%|-[\d.]+)?$|^\d")

UNIT_WORDS = {
    "גרם", "גר", 'ק"ג', "קג", "קילו", "קילוגרם", "קילוגרמים", "מל", 'מ"ל',
    "מיליליטר", "ליטר", "ליטרים", "יח", "יח'", "יחידה", "יחידות", "במארז",
    "קרטון", "מטר", "מטרים",
}

# Common enough to be filtered as filler, but they separate two real products.
PROTECTED = {
    "טרי", "קפוא", "אורגני", "מהדרין", "לבן", "אדום", "צהוב", "ירוק", "כתום",
    "סגול", "חריף", "שרי", "תמר", "מגי", "בייבי", "מיובש", "כבוש", "חמוץ",
}

# A packaged or prepared thing is not loose produce, whatever its name says.
# `רסק`, `סלט`, `מיץ` matter as much as `ארוז`: a jar of tomato paste must
# never land in the price comparison for a kilo of tomatoes.
PREPARED = re.compile(
    r"ארוז|מארז|מגש|שקית|קופס|יחיד|חבילה|מאגד|פרוס|קלופ|מיובש|ממולא|כבוש|"
    r"חמוץ|מוחמצ|רסק|מרוסק|רוטב|סלט|ממרח|מיץ|משקה|מסוכר|קלוי|מטוגן|טחון|"
    r"שימור|משומר|קפוא"
)

# Kilogram, written five ways by five chains. `unit_qty` and `unit_of_measure`
# disagree on 85% of weighed rows, so neither is trusted alone - see unit_of().
KILO = {'ק"ג', "קג", "קילו", "קילוגרם", "קילוגרמים", "1קילוגרם", "1 קילוגרם",
        "קילוגרם 1", 'ק"ג 1', "kg"}

PLURAL_ENDINGS = (("יות", "ה"), ("ות", "ה"), ("ות", ""), ("ים", ""), ("ים", "ה"))

# Prices inside one group vary by season and grade, but not by 4x. Anything
# further out is a different product wearing the same name, or a bad row - the
# 60.00 shekel "tomato" that was really frozen chicken breast.
OUTLIER_FACTOR = 4


def load_produce_words():
    with open(os.path.join(DATA, "produce_words.txt"), encoding="utf-8") as handle:
        return {line.strip() for line in handle
                if line.strip() and not line.startswith("#")}


def load_image_map():
    path = os.path.join(DATA, "pricez_images.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def collapse(word):
    """Fold the doubled yod/vav spelling onto the plain one."""
    for double, single in KTIV:
        word = word.replace(double, single)
    return word


def words(name):
    return [collapse(w) for w in WORD.findall(name or "")]


class Namer:
    """Name -> (specs, identifying words), given the whole catalogue's counts.

    The document frequencies have to come from every product, not just the
    weighed ones: `טרי` only looks like filler next to the 243k, and a word's
    rarity is the entire signal for it being the product rather than the
    packaging.
    """

    def __init__(self, all_names):
        self.df = Counter()
        total = 0
        for name in all_names:
            total += 1
            self.df.update(set(words(name)))
        self.filler_at = max(50, total // 100)
        self.stems = {}
        for word in self.df:
            for suffix, replacement in PLURAL_ENDINGS:
                if not word.endswith(suffix):
                    continue
                candidate = word[: -len(suffix)] + replacement
                if self.df.get(candidate, 0):
                    self.stems[word] = candidate
                    break

    def parse(self, name):
        specs, ident = set(), []
        for word in words(name):
            if word in UNIT_WORDS:
                continue
            if SPEC.match(word):
                specs.add(word)
                continue
            if len(word) < 2:
                continue
            stemmed = self.stems.get(word, word)
            if word in PROTECTED or stemmed in PROTECTED:
                ident.append(stemmed)
                continue
            if self.df.get(word, 0) >= self.filler_at:
                continue
            ident.append(stemmed)
        return specs, ident

    def key(self, name):
        """The group key: specs and identifying words, order-independent.

        Two names with the same key are the same product. Because parse()
        already folds plurals onto the singular that exists in the data,
        `עגבניות` and `עגבניה` produce the same key without any similarity
        threshold at grouping time - the threshold lives in the stemmer.
        """
        specs, ident = self.parse(name)
        if not ident:
            return None
        return " ".join(sorted(specs) + sorted(set(ident)))


def unit_of(unit_qty, unit_of_measure):
    """Kilogram, or None when the fields do not say so.

    Reported, never used as a filter. Requiring kilogram here is what dropped
    Carrefour from the tomato comparison entirely: it files loose tomatoes as
    `לא ידוע` / `100 גרם`, Osher Ad files them as `יחידות`, and neither is
    lying about selling tomatoes by weight - the unit field is simply noise.
    The two fields already disagree on 85% of weighed rows, so treating them as
    a gate threw away real chains to guard against a case the price band
    catches anyway.

    Worse, the merged products table holds one row per barcode, so the unit a
    barcode ends up with belongs to whichever chain sorted last. Filtering on
    it removed a barcode from EVERY chain because of one chain's typo.
    """
    for raw in (unit_qty, unit_of_measure):
        value = re.sub(r"[\d\s]", "", (raw or "").strip())
        if value in {re.sub(r"[\d\s]", "", k) for k in KILO}:
            return "kg"
    return None


def build(rows, all_names, extra=None, image_map=None, produce=None):
    """rows: (chain_id, barcode, name, unit_qty, unit_of_measure, price, count).

    One row per chain-and-barcode, carrying THAT chain's own name for the code.
    That matters more than it sounds: barcodes collide, so the merged catalogue
    holds one name per barcode and it belongs to whichever chain sorted last.
    Grouping on the merged name made Rami Levy's kilo of tomatoes disappear
    because another chain calls 7290000000100 a box of matches.

    `all_names` is every product name in the catalogue, which is what decides
    which words are filler - rarity only means anything against the whole.

    `extra` supplies the fallback: chains that price a member barcode but say
    nothing about it in their own file. Their prices still belong in the
    comparison - a chain pricing the national code for loose tomatoes is
    selling loose tomatoes - and staying silent is not the same as calling it
    something else, which is the case this whole per-chain reading exists to
    catch.

    Returns (generics, of_barcode).
    """
    produce = produce if produce is not None else load_produce_words()
    image_map = image_map if image_map is not None else load_image_map()

    namer = Namer(all_names)

    groups = defaultdict(lambda: {"barcodes": set(), "names": Counter(),
                                  "prices": defaultdict(list), "units": Counter()})
    for chain_id, barcode, name, unit_qty, measure, price, count in rows:
        name = (name or "").strip()
        if not name or PREPARED.search(name):
            continue
        specs, ident = namer.parse(name)
        # Produce names are one or two words. Three or more is a prepared dish
        # that happens to contain a vegetable - `חציל בטחינה`, `אפונה עם גזר`.
        if not ident or len(ident) > 2 or not (set(ident) & produce):
            continue
        key = namer.key(name)
        if not key:
            continue
        group = groups[key]
        group["barcodes"].add(barcode)
        group["names"][name] += 1
        group["units"][unit_of(unit_qty, measure)] += 1
        group["prices"][chain_id].append((price, count or 0, barcode))

    # Images were resolved by hand against Pricez under our label at the time.
    # Indexing them by key rather than by that label means a chain renaming its
    # tomato does not silently drop the picture.
    image_by_key = {}
    for label, entry in image_map.items():
        key = namer.key(label)
        if key:
            image_by_key.setdefault(key, entry.get("pricez_id"))

    # Silence is not disagreement. A chain with no weighed row of its own for a
    # member barcode joins on the strength of the other chains' reading; a
    # chain that files the same barcode as something else never reaches here,
    # because its own row put it in another group or in none.
    if extra:
        member_of = {}
        for key, group in groups.items():
            for barcode in group["barcodes"]:
                member_of.setdefault(barcode, key)
        for chain_id, barcode, price, count in extra:
            key = member_of.get(barcode)
            if key is None or chain_id in groups[key]["prices"]:
                continue
            groups[key]["prices"][chain_id].append((price, count or 0, barcode))

    generics, of_barcode = {}, {}
    for key, group in groups.items():
        # Cheapest first, then MOST branches: two members can carry the same
        # price, and the one 57 branches use describes the chain better than
        # the one 2 branches use - a plain min() picks by barcode digits.
        best = {c: min(v, key=lambda row: (row[0], -(row[1] or 0), row[2]))
                for c, v in group["prices"].items()}
        if not best:
            continue
        ordered = sorted(price for price, _, _ in best.values())
        median = ordered[len(ordered) // 2]
        kept = {c: row for c, row in best.items()
                if median / OUTLIER_FACTOR <= row[0] <= median * OUTLIER_FACTOR}
        if not kept:
            continue
        label = group["names"].most_common(1)[0][0]
        # Every other spelling the chains use for this same thing. The label is
        # one chain's wording; somebody typing `עגבניות` must still find a
        # group labelled `עגבניה`, and the plural only exists in the members.
        spellings = sorted({n for n in group["names"] if n != label})
        entry = {
            "n": label,
            "w": 1,
            # [price, branches at that price, the member barcode it came from]
            "p": {c: [round(price, 2), count or 0, src]
                  for c, (price, count, src) in kept.items()},
            "b": sorted(group["barcodes"]),
        }
        # Reported when any member says so, omitted when none does. The field
        # is no longer a gate, so it must not claim a unit the data never gave.
        if group["units"].get("kg"):
            entry["u"] = "kg"
        if spellings:
            entry["a"] = spellings
        image = image_by_key.get(key)
        if image:
            entry["i"] = image
        generics[key] = entry
        # A barcode can belong to different groups for different chains, since
        # the chains disagree about what it is. The pointer on the search shard
        # can only name one, so it names the group with the most chains behind
        # it - the reading most of the country agrees on.
        for barcode in group["barcodes"]:
            current = of_barcode.get(barcode)
            if current is None or len(kept) > len(generics[current]["p"]):
                of_barcode[barcode] = key
    return generics, of_barcode


def from_db(conn):
    """Read the merged database and group its weighed produce.

    chain_products is the per-chain view: what each chain itself calls a
    weighed barcode, joined to what that chain charges for it. Reading the
    merged products table instead is what let one chain's name for a colliding
    barcode decide the answer for every other chain.
    """
    rows = conn.execute("""
        SELECT cp.chain_id, cp.barcode, cp.name, cp.unit_qty, cp.unit_of_measure,
               p.price, p.store_count
        FROM chain_products cp
        JOIN chain_prices p ON p.chain_id = cp.chain_id AND p.barcode = cp.barcode
    """).fetchall()
    all_names = [n for (n,) in conn.execute(
        "SELECT name FROM products WHERE name <> ''")]
    # Chains that price a weighed barcode without describing it themselves.
    # Restricted to barcodes some chain does describe, which is what keeps this
    # to the produce aisle instead of the whole catalogue.
    extra = conn.execute("""
        SELECT p.chain_id, p.barcode, p.price, p.store_count
        FROM chain_prices p
        WHERE p.barcode IN (SELECT DISTINCT barcode FROM chain_products)
          AND NOT EXISTS (SELECT 1 FROM chain_products cp
                          WHERE cp.chain_id = p.chain_id AND cp.barcode = p.barcode)
    """).fetchall()
    return build(rows, all_names, extra)
