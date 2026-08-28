"""Scrape one chain and convert its XML to CSV.

Uses the two OpenIsraeliSupermarkets packages as intended: the scraper writes
XML into ``dumps/<Chain>/``, then the parser package - which has a dedicated
parser per chain - converts it to CSV in ``outputs/``.

Run one chain per process. A national scrape in a single job does not fit in a
GitHub runner's disk; the workflow fans this out over a job matrix.
"""

import argparse
import gzip
import os
import shutil
import sys

from il_supermarket_scarper import ScarpingTask, ScraperFactory
from il_supermarket_parsers import ConvertingTask

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import superpharm  # noqa: E402
from csvutil import find_csvs  # noqa: E402

# Full snapshots only. PRICE_FILE / PROMO_FILE are hourly *deltas* - fetching
# them means downloading the same store many times over and still ending up
# with a partial catalogue.
FILE_TYPES = ["PRICE_FULL_FILE", "STORE_FILE", "PROMO_FULL_FILE"]


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape and parse one supermarket chain.")
    parser.add_argument("chain", help="ScraperFactory name, e.g. RAMI_LEVY")
    parser.add_argument("--dumps", default="dumps")
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max files to download. Omit for everything.")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-chain scrape timeout in seconds.")
    return parser.parse_args()


def normalize_dump_extensions(dumps_dir):
    """Give every dump a .xml name, whatever the chain called the file.

    King Store's listing names its files ".GZ", and the library decides
    whether to add an extension with a case-sensitive test:

        if file_link.endswith((".gz", ".xml")) and ...

    ".GZ" fails it, so the download is written with no extension at all and
    never decompressed. Every file arrives intact and the chain still looks
    completely dead, because the count below only counts .xml.

    Sniffing the bytes rather than trusting the name fixes that chain and any
    other that spells its extension differently. Files the library already
    unpacked are left alone.
    """
    renamed = 0
    for root, _dirs, files in os.walk(dumps_dir):
        if os.path.basename(root) == "status":
            continue
        for name in files:
            if name.endswith((".xml", ".json")):
                continue
            path = os.path.join(root, name)
            target = os.path.join(root, name.rsplit(".", 1)[0] + ".xml")
            if os.path.exists(target):
                continue            # already unpacked under its proper name
            try:
                with open(path, "rb") as handle:
                    magic = handle.read(2)
                if magic == b"\x1f\x8b":
                    with gzip.open(path, "rb") as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.remove(path)
                elif magic.lstrip(b"\xef\xbb\xbf").startswith(b"<"):
                    os.rename(path, target)
                else:
                    continue
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                print(f"[fetch] could not unpack {name}: {exc}", file=sys.stderr)
                continue
            renamed += 1

    if renamed:
        print(f"[fetch] recovered {renamed} dump(s) saved without a .xml name")


def quarantine_unparsable_names(dumps_dir):
    """Move aside dumps whose *filename* the library cannot parse.

    Yohananof's listing carries four junk PromoFull files - chain id
    0000000000000, and a four-field name where every other file has five:

        PromoFull0000000000000-00-1-202411010501.xml

    The library reads the third field as the timestamp, gets "1", and raises.
    That happens while it is still *enumerating* files, so four stale promo
    files take down the entire chain's parse, prices included - which is how a
    120-file chain produced nothing at all.

    Skipping them here costs four junk files and saves the other 116. The
    quarantine folder is a sibling of dumps/, never inside it, or the parser
    would walk straight back into them.
    """
    from il_supermarket_parsers.utils.loading_utils import file_name_to_components

    doomed = []
    for root, _dirs, files in os.walk(dumps_dir):
        if os.path.basename(root) == "status":
            continue
        for name in files:
            if not name.endswith(".xml"):
                continue
            try:
                file_name_to_components(root, name)
            except Exception as exc:      # noqa: BLE001 - any failure disqualifies the file
                doomed.append((os.path.join(root, name), name, exc))

    if not doomed:
        return

    quarantine = dumps_dir.rstrip("/") + "_unparsable"
    os.makedirs(quarantine, exist_ok=True)
    for path, name, exc in doomed:
        shutil.move(path, os.path.join(quarantine, name))
        print(f"[fetch] skipping {name}: {exc}", file=sys.stderr)
    print(f"[fetch] quarantined {len(doomed)} file(s) the parser cannot name",
          file=sys.stderr)


def main():
    args = parse_args()

    known = ScraperFactory.all_scrapers_name()
    if args.chain not in known:
        print(f"Unknown chain {args.chain!r}.", file=sys.stderr)
        print("Known chains: " + ", ".join(sorted(known)), file=sys.stderr)
        return 2

    os.makedirs(args.dumps, exist_ok=True)
    os.makedirs(args.outputs, exist_ok=True)

    print(f"[fetch] scraping {args.chain} (file types: {', '.join(FILE_TYPES)})")
    scraper = ScarpingTask(
        # Names, not enum members: the filter does getattr(FileTypesFilters, x)
        # and getattr() on an enum member raises TypeError.
        enabled_scrapers=[args.chain],
        files_types=FILE_TYPES,
        multiprocessing=1,
        timeout_in_seconds=args.timeout,
        output_configuration={"output_mode": "disk", "base_storage_path": args.dumps},
        status_configuration={"database_type": "json",
                              "base_path": os.path.join(args.dumps, "status")},
    )
    scraper.start(limit=args.limit)
    scraper.join()          # start() returns immediately - without this the
                            # parser below runs against an empty folder.

    normalize_dump_extensions(args.dumps)

    downloaded = []
    for root, _dirs, files in os.walk(args.dumps):
        downloaded += [os.path.join(root, f) for f in files if f.endswith(".xml")]
    print(f"[fetch] {len(downloaded)} XML files on disk")
    if not downloaded:
        print(f"[fetch] {args.chain} produced no XML - failing loudly.", file=sys.stderr)
        return 1

    quarantine_unparsable_names(args.dumps)

    print(f"[fetch] parsing {args.chain}")
    if args.chain == "SUPER_PHARM":
        # Not the library's parser: it looks for a <Details> element Super-Pharm
        # no longer publishes and writes an empty result without erroring.
        # scripts/superpharm.py explains why overriding it is not workable.
        superpharm.convert(args.dumps, args.outputs, FILE_TYPES)
    else:
        converter = ConvertingTask(
            enabled_parsers=[args.chain],
            files_types=FILE_TYPES,
            source_configuration={"folder": args.dumps},
            output_configuration=[{"output_mode": "csv", "output_folder": args.outputs}],
            status_configuration={"database_type": "json",
                                  "base_path": os.path.join(args.outputs, "status")},
        )
        converter.start()
        converter.join()

    prices = find_csvs(args.outputs, "PRICE_FULL_FILE")
    if not prices:
        print(f"[fetch] no price CSV written for {args.chain}.", file=sys.stderr)
        print("        outputs/: " + ", ".join(sorted(os.listdir(args.outputs))), file=sys.stderr)
        return 1

    for path in sorted(os.listdir(args.outputs)):
        full = os.path.join(args.outputs, path)
        if os.path.isfile(full):
            print(f"[fetch] {path}  {os.path.getsize(full) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
