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

**It is behind Cloudflare, and Cloudflare reads the User-Agent.** A request
with the ``python-requests`` or ``curl`` default UA gets 403; an empty UA, a
browser UA and the literal string "grocery-price-data" all get 200. So this is
a bot-UA rule rather than a datacenter-IP block, which is why the chain is not
in ``HOME_EGRESS`` - but it does mean a request must never go out with the
library's default header.

**One page holds everything.** The index lists every file for the current day -
563 of them - with no pagination and no date argument we need. It also lists
the hourly ``Price``/``Promo`` deltas, which we do not want: FULL snapshots
only, for the reason given at the top of fetch.py.
"""

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


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def list_files(session, timeout=60):
    """Every full-snapshot filename the portal is offering right now."""
    response = session.get(INDEX, timeout=timeout)
    response.raise_for_status()
    names = sorted(set(_LINK.findall(response.text)))
    if not names:
        raise RuntimeError(
            "netiv-hesed served a page with no download links - the portal "
            "changed shape, or Cloudflare served a challenge instead")
    return [name for name in names if WANTED.match(name)]


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


def scrape(dumps_dir, limit=None):
    """Download the chain's current full snapshots into ``dumps_dir``.

    Returns the number of files written. Mirrors what ``ScarpingTask`` would
    have left behind, so fetch.py can carry on as it does for every other
    chain.
    """
    target_dir = os.path.join(dumps_dir, FOLDER)
    os.makedirs(target_dir, exist_ok=True)

    session = _session()
    names = list_files(session)
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
