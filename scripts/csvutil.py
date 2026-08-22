"""Reading the CSVs produced by il_supermarket_parsers.

Two quirks of that output are handled here:

1. Column names are lower-cased versions of the XML tags (``itemcode``,
   ``storeid``, ...), and differ slightly between chains.
2. ``CSVOutputWriter`` runs with ``reduce_duplicates=True``, so a cell that
   repeats the value of the row above is written empty and has to be
   forward-filled. A genuinely empty value is written as the literal ``''``.
"""

import csv
import glob
import os
import sys

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

EMPTY = "''"


def find_csvs(outputs_dir, file_type):
    """Locate the CSVs for one file type, whatever the chain suffix is.

    ``file_type`` is a FileTypesFilters name, e.g. ``PRICE_FULL_FILE``.
    """
    exact = os.path.join(outputs_dir, f"{file_type.lower()}_*.csv")
    hits = sorted(glob.glob(exact))
    if hits:
        return hits
    # Older/newer versions of the writer have used a squashed spelling
    # (``pricefull_*.csv``). Accept that too rather than silently finding nothing.
    squashed = file_type.lower().replace("_file", "").replace("_", "")
    return sorted(glob.glob(os.path.join(outputs_dir, f"{squashed}_*.csv")))


def read_rows(path):
    """Yield dicts with lower-cased keys, duplicate-masking undone."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [c.strip().lower() for c in next(reader)]
        except StopIteration:
            return
        carry = {}
        for raw in reader:
            if not raw:
                continue
            row = {}
            for idx, name in enumerate(header):
                value = raw[idx] if idx < len(raw) else ""
                if value == "":
                    value = carry.get(name, "")   # masked duplicate -> forward fill
                elif value == EMPTY:
                    value = ""                     # explicit empty
                    carry[name] = ""
                else:
                    carry[name] = value
                row[name] = value
            yield row


def pick(row, *candidates, default=""):
    """First present, non-empty column among ``candidates``."""
    for name in candidates:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def digits(value):
    """Keep only digits - barcodes arrive zero-padded and sometimes quoted."""
    return "".join(ch for ch in str(value) if ch.isdigit())


def to_float(value):
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
