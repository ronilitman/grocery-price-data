"""Scrape one chain and convert its XML to CSV.

Uses the two OpenIsraeliSupermarkets packages as intended: the scraper writes
XML into ``dumps/<Chain>/``, then the parser package - which has a dedicated
parser per chain - converts it to CSV in ``outputs/``.

Run one chain per process. A national scrape in a single job does not fit in a
GitHub runner's disk; the workflow fans this out over a job matrix.
"""

import argparse
import os
import sys

from il_supermarket_scarper import ScarpingTask, ScraperFactory
from il_supermarket_parsers import ConvertingTask

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

    downloaded = []
    for root, _dirs, files in os.walk(args.dumps):
        downloaded += [os.path.join(root, f) for f in files if f.endswith(".xml")]
    print(f"[fetch] {len(downloaded)} XML files on disk")
    if not downloaded:
        print(f"[fetch] {args.chain} produced no XML - failing loudly.", file=sys.stderr)
        return 1

    print(f"[fetch] parsing {args.chain}")
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
