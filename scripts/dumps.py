"""Keep only each branch's newest dump, and throw the superseded ones away.

The chains do not serve one file per branch, they serve a window of them.
Super-Pharm keeps about three days, Shufersal was still offering a file from
15 July, and Yohananof lists files from 2024 at the top of its directory. The
scraper takes everything the listing offers, so a run downloads the same branch
several times over - 909 PromoFull dumps for Super-Pharm's 307 branches.

Nothing downstream reconciles that. ``promos.read_offers`` walks every file and
adds each one's ``StoreID`` to the offer's branch set, and no branch is ever
removed from it. So an offer a branch stopped running days ago stays attached
to that branch for as long as an old copy of its file is still being served.
That is not hypothetical: on 6 September the app told a shopper that Slim
Delice was ₪10 at Super-Pharm Givat Shmuel, from a promotion that branch had
dropped on the 4th and which we were still reading out of the 3rd's file.

Expiry does not save us. ``build_catalog`` drops offers whose end date has
passed, which is why files from 2024 do no harm - but the ₪10 offer ran until
23 September, so it sailed through.

Two rules, and the second matters more than the first:

  * A file's branch is read from the XML - ``<ChainID>`` and ``<StoreID>`` -
    never from its name. The name cannot be trusted for this: the same field
    is ``-000-152-`` at Super-Pharm, ``-066-`` at Bareket and ``-00-1-`` at
    Yohananof, whose filename also claims the chain is 0000000000000 while the
    document inside it says otherwise.

  * A file we cannot date is always kept. Filenames carry one of two stamps -
    ``20260907-071543`` or ``202609070502`` - and one pattern reads both; all
    30 files sampled across all 31 chains parsed. But that is one sample per
    chain, so anything unrecognised has to survive rather than be guessed at.
    A chain whose names we cannot read keeps every file and behaves exactly as
    it did before this existed.
"""

import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

# The last date-shaped run in the name, with or without the separator. Anchored
# on "no more digits after it" so a chain id at the front is never mistaken for
# a timestamp.
_STAMP = re.compile(r"(\d{8})[-_]?(\d{4,6})(?!.*\d)")

# PriceFull, PromoFull and Stores each get their own bucket: a branch publishes
# one of each and they must not evict one another.
_KIND = re.compile(r"^([A-Za-z]+?)\d", re.ASCII)


def file_stamp(name):
    """``YYYYMMDDHHMMSS`` from a dump's name, or None if it does not say."""
    match = _STAMP.search(name)
    if not match:
        return None
    return match.group(1) + match.group(2).ljust(6, "0")


def file_kind(name):
    """``pricefull`` / ``promofull`` / ``stores``, or None."""
    match = _KIND.match(os.path.basename(name))
    return match.group(1).lower() if match else None


def identity(path):
    """``(chain_id, store_id)`` from the file's own header, or None.

    Stops as soon as it has both. Every dialect puts them above the item list,
    so this reads a few hundred bytes of a file that may be several megabytes.
    """
    chain = store = None
    try:
        for _event, element in ET.iterparse(path, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if chain is None and tag in ("ChainID", "ChainId"):
                chain = (element.text or "").strip()
            elif store is None and tag in ("StoreID", "StoreId"):
                store = (element.text or "").strip()
            element.clear()
            if chain and store:
                break
    except (ET.ParseError, OSError):
        return None
    if not chain or not store:
        return None                        # a Stores file, or an unreadable one
    return ("".join(c for c in chain if c.isdigit()),
            str(store).lstrip("0") or "0")


def superseded(paths):
    """Which of `paths` an older copy makes redundant."""
    newest = {}
    dated = defaultdict(list)
    for path in paths:
        stamp = file_stamp(os.path.basename(path))
        if stamp is None:
            continue                       # undatable: never a candidate to drop
        who = identity(path)
        if who is None:
            continue                       # unreadable header: same
        key = (file_kind(path), *who)
        dated[key].append((stamp, path))
        newest[key] = max(newest.get(key, ""), stamp)

    drop = []
    for key, entries in dated.items():
        for stamp, path in entries:
            # Ties are kept, both of them. Two files bearing the same stamp are
            # a chain republishing within the minute its name records, and
            # there is nothing in the name to choose between them.
            if stamp < newest[key]:
                drop.append(path)
    return drop


def prune_to_newest(dumps_dir):
    """Delete every dump an newer copy of the same branch supersedes.

    Returns ``(kept, removed)``. Deleting rather than filtering a list because
    two different readers walk this directory - the parser package for prices
    and promos.py for promotions - and a filter one of them did not apply would
    leave the two disagreeing about what the chain published.
    """
    paths = []
    for root, _dirs, files in os.walk(dumps_dir):
        if os.path.basename(root) in ("status", "outputs"):
            continue
        paths += [os.path.join(root, f) for f in files if f.lower().endswith(".xml")]

    drop = superseded(paths)
    for path in drop:
        try:
            os.remove(path)
        except OSError as err:
            print(f"[dumps] could not remove {os.path.basename(path)}: {err}")
    kept = len(paths) - len(drop)
    print(f"[dumps] {len(paths)} dumps -> {kept} kept, {len(drop)} superseded")
    return kept, len(drop)
