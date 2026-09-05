"""Tell Roni when something he buys goes on discount at a branch he shops.

Reads the *published* catalogue rather than the merged database. The database
is deleted at the end of the publish job - it outgrew the release limit and the
site reads JSON shards regardless - so there is nothing to hand a downstream
job. Reading the published JSON also means an alert says exactly what the app
would say, and that the whole thing runs on a laptop against the live site.

    favourites (Firestore) x promo shards -> new offers -> Telegram
                                  |
                             alert ledger (Firestore)

WHAT COUNTS AS "THE SAME OFFER"

Not the promotion id. Chains reissue an unchanged deal under a fresh id every
campaign: between 3 and 4 September, Super-Pharm replaced 911 of its 1,000
promotion ids while 5,302 offers carried through untouched, and 3,702 of those
came back wearing a new id. A ledger keyed on the id would have alerted on all
3,702. ``build_chain_db.py`` says the same thing from the other side - the
identity of an offer is its terms, which is why promo_id is deliberately not
part of ``idx_offer_key``.

So the key is the terms, and it is the published equivalent of that index:
chain, barcode, club, coupon, min quantity, price.

LEDGER LIFETIME

A row lives until the deal it describes ends, then goes. That is what lets the
same deal alert again next season, and it is why ``ends`` is refreshed on every
sighting: chains extend campaigns without changing terms, and a row still
holding the original date would be swept while the deal was running, making a
deal that never stopped look new the next night.

``ends`` is refreshed on the row rather than folded into the key on purpose. In
the key, an extension would itself read as a new offer.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://ronilitman.github.io/grocery-price-data/catalog"
ROOM = os.environ.get("ALERTS_ROOM", "our-groceries")
LEDGER = "alerts_offers"

# An offer priced under this fraction of the chain's own shelf price is not
# reported. It is a guard on a push notification, not a fix for the data - see
# the module docstring in promos.py for what these actually are. Three shapes
# produce them and none is a bargain: a promotion whose item rows encode
# "second one for a shekel" (Super-Pharm files the 28.90 and the 1.00 as
# separate rows under one id, so the cheaper row alone reads as a 1.00
# mouthwash); a percentage discount filed with a large MinQty, which divides
# out to nonsense; and a chain hanging one product's headline on a long item
# list, which is how peanut butter ends up priced as a vanilla pudding.
# Roughly 1.2% of published offers trip this. Fixing them belongs in the
# parser; keeping them out of a notification belongs here.
IMPLAUSIBLE = 0.2

# Below this the ratio test is meaningless - a 3 shekel item legitimately
# discounted to 50 agorot is not suspicious.
GUARD_FLOOR = 5.0


# --------------------------------------------------------------- fetching

_cache = {}


def get_json(path):
    if path not in _cache:
        with urllib.request.urlopen(f"{BASE}/{path}", timeout=60) as r:
            _cache[path] = json.load(r)
    return _cache[path]


def shard_for(barcode, index):
    """Longest prefix in the index that this barcode starts with.

    Mirrors ``shardFor`` in the app's prices.js - short barcodes are padded
    with zeros, and the longest matching level wins.
    """
    shards = set(index["shards"])
    for level in sorted(index["levels"], reverse=True):
        prefix = barcode[:level].ljust(level, "0")
        if prefix in shards:
            return prefix
    return None


def barcode_variants(barcode):
    """A barcode with and without its ``729000`` prefix.

    CLAUDE.md: chains publish the same product both ways, so a lookup that
    misses should try the other form before concluding the product is unknown.
    """
    seen = [barcode]
    if barcode.startswith("729000") and len(barcode) > 6:
        seen.append(barcode[6:])
    else:
        seen.append("729000" + barcode)
    return seen


# --------------------------------------------------------------- the offer

def offer_key(chain_id, barcode, offer):
    """The published equivalent of ``idx_offer_key``.

    ``t`` is the headline total for a multi-buy ("2 for 34") and ``u`` is the
    per-unit price; when there is no ``q`` the two are the same number. Club and
    coupon are part of the identity because a coupon at 12.90 and a shelf
    discount at 12.90 are not the same deal.
    """
    min_qty = offer.get("q", 1)
    price = offer.get("t", offer["u"])
    club = 1 if offer.get("c") else 0
    coupon = 1 if offer.get("k") else 0
    raw = f"{chain_id}|{barcode}|{club}|{coupon}|{min_qty}|{price}"
    return hashlib.sha1(raw.encode()).hexdigest()


def runs_at(offer, store_id):
    """Does this offer run at that branch?

    ``s`` lists the branches honouring it and ``x`` the ones excluded; whichever
    is shorter is published, and neither appears when the whole chain honours
    it. Absent both, it runs everywhere.
    """
    if "s" in offer:
        return store_id in offer["s"]
    if "x" in offer:
        return store_id not in offer["x"]
    return True


def implausible(offer, shelf):
    if not shelf or shelf < GUARD_FLOOR:
        return False
    return offer["u"] < shelf * IMPLAUSIBLE


# --------------------------------------------------------------- favourites

def load_favourites(args):
    """Barcodes and branches, from Firestore or from a file when testing."""
    if args.favourites_file:
        data = json.load(open(args.favourites_file))
        return data["barcodes"], data["stores"]

    from google.cloud import firestore
    db = firestore.Client()
    barcodes = []
    for doc in db.collection(f"favproducts_{ROOM}").stream():
        bc = (doc.to_dict() or {}).get("barcode")
        if bc:
            barcodes.append(bc)
    prefs = db.collection(f"settings_{ROOM}").document("prefs").get()
    stores = (prefs.to_dict() or {}).get("favouriteStores", []) if prefs.exists else []
    return barcodes, [(s["chainId"], s["storeId"]) for s in stores]


# --------------------------------------------------------------- the ledger

class FileLedger:
    """A JSON file, for running this on a laptop without credentials."""

    def __init__(self, path):
        self.path = path
        self.rows = json.load(open(path)) if os.path.exists(path) else {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, key, row):
        self.rows[key] = row

    def sweep(self, today):
        gone = [k for k, v in self.rows.items() if (v.get("ends") or "9999") < today]
        for k in gone:
            del self.rows[k]
        return len(gone)

    def clear(self):
        n = len(self.rows)
        self.rows = {}
        return n

    def commit(self):
        json.dump(self.rows, open(self.path, "w"), ensure_ascii=False, indent=1)


class FirestoreLedger:
    def __init__(self):
        from google.cloud import firestore
        self.db = firestore.Client()
        self.col = self.db.collection(LEDGER)
        # One read of the whole ledger beats one read per favourite offer, and
        # the ledger is small by construction - it only ever holds live offers
        # on favourited barcodes.
        self.rows = {d.id: d.to_dict() for d in self.col.stream()}
        self.pending = {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, key, row):
        self.rows[key] = row
        self.pending[key] = row

    def sweep(self, today):
        gone = [k for k, v in self.rows.items() if (v.get("ends") or "9999") < today]
        for key in gone:
            self.col.document(key).delete()
            del self.rows[key]
            self.pending.pop(key, None)
        return len(gone)

    def clear(self):
        """Empty the ledger so the next run alerts on everything live.

        Only ever touches the alert ledger. The collection name is a module
        constant rather than an argument precisely so this cannot be pointed at
        favproducts_* or settings_*, which are a real shopping list.
        """
        n = 0
        for doc in self.col.stream():
            doc.reference.delete()
            n += 1
        self.rows.clear()
        self.pending.clear()
        return n

    def commit(self):
        # Firestore caps a batch at 500 writes; the ledger is far smaller than
        # that, but chunking costs nothing and removes the cliff.
        keys = list(self.pending)
        for i in range(0, len(keys), 400):
            batch = self.db.batch()
            for key in keys[i:i + 400]:
                batch.set(self.col.document(key), self.pending[key])
            batch.commit()
        self.pending.clear()


# --------------------------------------------------------------- delivery

def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set"
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        # The token is in the URL, so str(exc) is never printed; the response
        # body is Telegram's own diagnosis and carries no secret.
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = "(no body)"
        return f"HTTP {exc.code} — {detail}"
    except Exception as exc:
        return f"request failed: {type(exc).__name__}"
    return None


def escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Telegram rejects a message over 4096 characters. Splitting at 3600 leaves
# room for the continuation header and never lands mid-offer.
TELEGRAM_LIMIT = 3600


def offer_lines(a):
    """One offer as two lines: the price, then the terms under it.

    ``q`` is always printed beside ``u``: "₪14.95" alone, for a two-for deal,
    is the same misreading as the ₪1 mouthwash this script guards against.
    The branch is named only when the offer does not run at every branch of the
    chain, since under a chain heading it would otherwise repeat on every line.
    """
    terms = []
    if a["min_qty"] and a["min_qty"] != 1:
        terms.append(f"{a['min_qty']:g} ב-{a['price']:g}")
    if a["club"]:
        terms.append("מועדון")
    if a["coupon"]:
        terms.append("קופון")
    if a.get("branch_specific"):
        terms.append(escape(a["branch"]))
    terms.append(f"עד {a['ends'][8:10]}.{a['ends'][5:7]}")
    return [f"<b>₪{a['unit_price']:g}</b> {escape(a['name'])}",
            f"   <i>{' · '.join(terms)}</i>"]


def render(alerts):
    """Every offer, grouped by chain, as one or more messages.

    Grouped by chain rather than by branch because an offer frequently runs at
    both of the Shufersal branches, and filing it under one of them arbitrarily
    is what made a flat list read as repetition. Chains are ordered by their
    cheapest find, so the best thing in the message is near the top.
    """
    by_chain = {}
    for a in alerts:
        by_chain.setdefault(a["chain_name"], []).append(a)
    order = sorted(by_chain, key=lambda c: min(x["unit_price"] for x in by_chain[c]))

    blocks = []
    for chain in order:
        rows = sorted(by_chain[chain], key=lambda x: x["unit_price"])
        block = [f"🏬 <b>{escape(chain)}</b> · {len(rows)}"]
        for a in rows:
            block += offer_lines(a)
        blocks.append("\n".join(block))

    head = f"🏷️ <b>{len(alerts)} new discount{'s' if len(alerts) != 1 else ''} on your favourites</b>"
    messages, current = [], [head]
    for block in blocks:
        candidate = "\n\n".join(current + [block])
        if len(candidate) > TELEGRAM_LIMIT and len(current) > 1:
            messages.append("\n\n".join(current))
            current = [block]
        else:
            current.append(block)
    messages.append("\n\n".join(current))

    if len(messages) > 1:
        messages = [m if i == 0 else f"🏷️ <i>({i + 1}/{len(messages)})</i>\n\n{m}"
                    for i, m in enumerate(messages)]
    return messages


# --------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", action="store_true",
                    help="Record everything as seen and send only a count. Use once.")
    ap.add_argument("--reset", action="store_true",
                    help="Empty the ledger first, so this run alerts on every live offer.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be sent; write nothing, send nothing.")
    ap.add_argument("--favourites-file", help="Test without Firestore.")
    ap.add_argument("--ledger-file", help="Use a JSON file as the ledger instead of Firestore.")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    index = get_json("index.json")

    # Alerting off a stale build would either repeat yesterday or, worse, look
    # like nothing is ever on offer. The publish job can go red while the site
    # deploys fine, so the build's own timestamp is what to trust.
    if index["built_at"][:10] != today:
        print(f"catalogue was built {index['built_at']}, not today — nothing to do")
        return 0

    barcodes, stores = load_favourites(args)
    chains_wanted = {}
    for chain_id, store_id in stores:
        chains_wanted.setdefault(chain_id, set()).add(store_id)
    print(f"{len(barcodes)} favourites, {len(stores)} branches across {len(chains_wanted)} chains")

    promo_index = get_json("promo/index.json")
    store_names = get_json("stores.json")
    chain_names = index.get("chains", {})

    ledger = FileLedger(args.ledger_file) if args.ledger_file else (
        None if args.dry_run and not args.ledger_file else FirestoreLedger())
    if ledger is None:
        ledger = FileLedger(os.devnull + ".json")  # nothing persists in a bare dry run

    if args.reset and not args.dry_run:
        print(f"cleared {ledger.clear()} ledger rows — this run alerts on everything live")

    alerts, suppressed, seen = [], 0, 0

    for barcode in barcodes:
        for variant in barcode_variants(barcode):
            prefix = shard_for(variant, promo_index)
            if not prefix:
                continue
            row = get_json(f"promo/{prefix}.json").get(variant)
            if not row:
                continue

            search_prefix = shard_for(variant, index)
            product = get_json(f"{search_prefix}.json").get(variant, {}) if search_prefix else {}
            name = product.get("n") or variant

            for chain_id, offers in row.items():
                if chain_id not in chains_wanted:
                    continue
                shelf = (product.get("p") or {}).get(chain_id, [None])[0]

                for offer in offers:
                    if offer.get("e") and offer["e"] < today:
                        continue                       # already over
                    if offer.get("b") and offer["b"] > today:
                        continue                       # announced, not yet live
                    branches = [s for s in chains_wanted[chain_id] if runs_at(offer, s)]
                    if not branches:
                        continue                       # not at a branch he shops
                    if implausible(offer, shelf):
                        suppressed += 1
                        continue

                    key = offer_key(chain_id, variant, offer)
                    ends = offer.get("e") or "9999-12-31"
                    existing = ledger.get(key)
                    if existing:
                        seen += 1
                        # Refresh, never re-alert. Without this the sweep would
                        # drop an extended campaign mid-run and re-announce it.
                        ledger.put(key, {**existing,
                                         "ends": max(existing.get("ends", ""), ends),
                                         "last_seen": today})
                        continue

                    branch_id = sorted(branches)[0]
                    branch = (store_names.get(chain_id, {}).get(branch_id) or [branch_id])[0]
                    # Naming the branch only earns its place when the offer does
                    # not run chain-wide; otherwise it repeats under every line
                    # of a chain heading that already says it.
                    branch_specific = "s" in offer or "x" in offer
                    record = {
                        "chain_id": chain_id, "barcode": variant,
                        "unit_price": offer["u"], "min_qty": offer.get("q", 1),
                        "price": offer.get("t", offer["u"]),
                        "club": 1 if offer.get("c") else 0,
                        "coupon": 1 if offer.get("k") else 0,
                        "ends": ends, "first_seen": today, "last_seen": today,
                    }
                    ledger.put(key, record)
                    alerts.append({**record, "name": name, "branch": branch,
                                   "branch_specific": branch_specific,
                                   "chain_name": chain_names.get(chain_id, chain_id)})
            break   # this variant matched; do not also look up the other form

    swept = ledger.sweep(today)
    alerts.sort(key=lambda a: a["unit_price"])

    print(f"new={len(alerts)}  already known={seen}  suppressed as implausible={suppressed}  swept={swept}")

    if args.dry_run:
        print("\n--- would send ---")
        print("\n\n=== next message ===\n\n".join(render(alerts)) if alerts else "(nothing)")
        return 0

    if args.seed:
        ledger.commit()
        problem = telegram(f"🔔 <b>Alert ledger seeded</b>\n\n"
                           f"{len(alerts)} live offers on your favourites recorded as already seen.\n"
                           f"From tomorrow you'll only hear about new ones.")
        if problem:
            print(f"Telegram failed — {problem}", file=sys.stderr)
            return 1
        return 0

    if alerts:
        for message in render(alerts):
            problem = telegram(message)
            if problem:
                # Do not commit: an alert the ledger records but never delivers
                # is an alert lost for good. Stop at the first refusal rather
                # than pressing on - the rest will fail the same way.
                print(f"Telegram failed — {problem}", file=sys.stderr)
                return 1

    ledger.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
