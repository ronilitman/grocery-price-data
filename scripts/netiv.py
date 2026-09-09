"""Scrape נתיב החסד - and with it בר-כל and שירה מרקט - from its own portal.

The library ships a NETIV_HASED scraper but it points at ``141.226.203.152``,
which has answered HTTP 500 for long enough that the library disables the
chain outright: it is absent from ``ScraperFactory.all_scrapers_name()``, so
even asking for it by name gets nothing. The chain did not stop publishing, it
moved - gov.il now lists ``app.netiv-hesed.com``, whose own header reads
"אתר שקיפות מחירים נתיב החסד, ברכלטוב, שירה מרקט".

That is the only reason this file exists. Everything downstream is the
library's: the parser package still has a working NETIV_HASED parser, and the
file dialect it emits parses without a single override.

Two things about the portal are worth knowing before changing anything here.

**It is behind Cloudflare, and Cloudflare objects twice.** From a home
connection the ``python-requests`` and ``curl`` default UAs get 403 while an
empty UA, a browser UA and the literal string "grocery-price-data" all get
200 - so a request must never go out with the library's default header. That
looked like the whole rule and it is not: a GitHub runner is refused whatever
UA it sends, which is why the chain is in ``HOME_EGRESS`` and leaves through
the exit node. Both defences are live; satisfying one does not settle the
other.

**One page holds one day.** The index lists every file that day published -
roughly 560 - with no pagination, and a ``Date`` parameter asks for an earlier
one. It shows today by default, which is empty of snapshots until the chain
publishes them; see ``newest_day_with_files``. It also lists the hourly
``Price``/``Promo`` deltas, which we do not want: FULL snapshots only, for the
reason given at the top of fetch.py.
"""

import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://app.netiv-hesed.com"
INDEX = BASE + "/"
DOWNLOAD = BASE + "/Prices/Download"

# The chain's dump folder is the library's name for it, because the parser
# package finds files by walking dumps/ and keys its output on that folder.
FOLDER = "NetivHased"

# What fetch.py and the parser factory call this chain.
CHAIN = "NETIV_HASED"

# Full snapshots only, matching fetch.FILE_TYPES. Anchored at the start of the
# name so "Price7290..." (an hourly delta) can never satisfy "PriceFull".
WANTED = re.compile(r"^(PriceFull|PromoFull|Stores)", re.IGNORECASE)

_LINK = re.compile(r"/Prices/Download\?fileName=([^\"'&<>]+)")

# Anything but a bot's default. See the Cloudflare note above.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

THREADS = 5
ATTEMPTS = 3

# How far back to look when today's snapshot has not been published yet. Three
# days rather than the artifact retention's seven: past that, the chain has a
# real problem and the previous night's artifact - which backfill_chains.py
# carries forward, correctly labelled - is no worse and needs no excuses.
LOOKBACK_DAYS = 3


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


NO_LISTING = ("netiv-hesed served a page with no download links at all - the "
              "portal changed shape, or Cloudflare served a challenge instead")


def _listing(session, day=None, timeout=60):
    """``(every download link, the full-snapshot ones)`` for one day.

    Both halves matter, and they mean different things. A page carrying links
    but no snapshot is the chain not having published yet - an ordinary state
    of the world, several hours long, every day. A page carrying no links at
    all is not a listing: the portal changed shape, or Cloudflare answered
    with a challenge. Only the caller sees enough days to tell those apart.
    """
    params = {"Date": day.isoformat()} if day else None
    response = session.get(INDEX, params=params, timeout=timeout)
    response.raise_for_status()
    names = sorted(set(_LINK.findall(response.text)))
    return names, [name for name in names if WANTED.match(name)]


def list_files(session, day=None, timeout=60):
    """Every full-snapshot filename the portal offers for ``day``.

    ``day`` is a ``datetime.date``; omit it for whatever the portal shows by
    default, which is today.
    """
    names, full = _listing(session, day=day, timeout=timeout)
    if not names and not day:
        raise RuntimeError(NO_LISTING)
    return full


def newest_day_with_files(session, today=None, lookback=LOOKBACK_DAYS):
    """The most recent day the portal has full snapshots for, and its files.

    Every other chain gets this for free. Their portals serve a rolling window
    - Super-Pharm keeps about three days, Shufersal has offered files weeks old
    - so a morning where today's snapshot has not landed yet still lists
    yesterday's, and dumps.prune_to_newest drops whatever is superseded. This
    portal shows one day at a time, so between the midnight rollover and
    whenever the full set is published its listing holds nothing but hourly
    deltas, and asking only for today means coming back empty.

    That is not hypothetical. The chain normally publishes at about 05:25; on
    9 September 2026 it published at 16:59, and the 07:26 build found a page
    full of deltas and no snapshot at all - the chain's first night on the
    site, so there was no artifact to carry forward either and it simply did
    not appear.

    Returns ``(day, names)``, where ``day`` is None if the portal's own default
    listing had the files and no date had to be asked for. Days are the
    portal's, not ours: it stamps its files in Israel local time and we only
    ever hand back what it agreed to serve.

    A day with no links at all is not treated as the end of the road - the
    chain does skip days, and a page with nothing on it looks the same as one
    Cloudflare intercepted. That only becomes an error if *no* day answered
    with a listing, which is the point at which "we are not being served" is
    the only reading left.
    """
    served = False
    names, full = _listing(session)
    served |= bool(names)
    if full:
        return None, full

    today = today or datetime.date.today()
    for age in range(1, lookback + 1):
        day = today - datetime.timedelta(days=age)
        names, full = _listing(session, day=day)
        served |= bool(names)
        if full:
            return day, full

    if not served:
        raise RuntimeError(NO_LISTING)
    return None, []


def _download(session, name, target_dir, timeout=120):
    """Fetch one file, returning its size, or None if it could not be had."""
    path = os.path.join(target_dir, name)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = session.get(DOWNLOAD, params={"fileName": name},
                                   timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("empty body")
            # Written under the portal's own name - ".GZ" and all. fetch.py's
            # normalize_dump_extensions sniffs the magic bytes and unpacks it,
            # the same path that rescued King Store's ".GZ" listing.
            with open(path, "wb") as handle:
                handle.write(response.content)
            return len(response.content)
        except Exception as exc:                       # noqa: BLE001
            if attempt == ATTEMPTS:
                print(f"[netiv] giving up on {name}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 * attempt)
    return None


def record_as_of(published_on, chain_dbs, chain=CHAIN):
    """Leave a note when tonight's files are not actually from tonight.

    backfill_chains.py stamps every chain a run built with *now*, which is
    right for a chain whose portal served today's snapshot and wrong for one
    that had to reach back a day. Without this the app would print yesterday's
    prices with no "1d old" beside them - stale prices are labelled, not
    hidden, and a scrape that quietly reaches back is exactly what can break
    that.

    Nothing is written when the files are today's, so the ordinary path is
    untouched. The note goes beside the chain databases because that is what
    travels to the publish job in the artifact.
    """
    if not published_on:
        return None
    os.makedirs(chain_dbs, exist_ok=True)
    path = os.path.join(chain_dbs, f"_asof_{chain}.json")
    # Midday, not midnight: this is only ever rendered as an age in days, and
    # a midnight stamp turns "1d" into "2d" a few hours early in Israel.
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({chain: f"{published_on.isoformat()}T12:00:00+00:00"},
                  handle, indent=2)
    return path


def scrape(dumps_dir, limit=None, chain_dbs="chain_dbs"):
    """Download the chain's newest full snapshots into ``dumps_dir``.

    Returns the number of files written, and mirrors what ``ScarpingTask``
    would have left behind, so fetch.py can carry on as it does for every
    other chain.

    If today's snapshot is not published yet this takes the newest day that is,
    and writes the date down next to the chain databases. Prices from yesterday
    published as though they were tonight's would be worse than the chain
    being missing, which is the only reason the fallback is allowed at all.
    """
    target_dir = os.path.join(dumps_dir, FOLDER)
    os.makedirs(target_dir, exist_ok=True)

    session = _session()
    day, names = newest_day_with_files(session)
    if day:
        print(f"[netiv] today's snapshot is not published yet - "
              f"falling back to {day.isoformat()}")
        note = record_as_of(day, chain_dbs)
        print(f"[netiv] recorded that age in {os.path.basename(note)}")
    print(f"[netiv] portal is offering {len(names)} full-snapshot files")
    if limit:
        # A store file in a limited run keeps the smoke test honest: without
        # it every branch is nameless and the failure looks like the chain's.
        stores = [n for n in names if n.lower().startswith("stores")]
        names = stores + [n for n in names if n not in stores][:limit]

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        sizes = list(pool.map(lambda n: _download(session, n, target_dir), names))

    written = [size for size in sizes if size]
    print(f"[netiv] downloaded {len(written)}/{len(names)} files, "
          f"{sum(written) / 1e6:.1f} MB")
    return len(written)
