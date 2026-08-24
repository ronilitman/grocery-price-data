"""Super-Pharm XML -> the same CSVs il_supermarket_parsers would have written.

Why this exists instead of a parser override: the upstream Super-Pharm parser
(``il_supermarket_parsers/parsers/super_pharm.py``) still looks for a
``<Details>`` element, but the chain publishes ``<Items>`` for prices and
``<Promotions>`` for promos. It finds nothing, writes no CSV, and reports
``errors: False`` while doing it - a silent zero.

Patching it from here is not possible cleanly. ``ParserFactory`` is an Enum
holding the converter *classes*, so the member cannot be reassigned, and the
converter is constructed inside a worker process from a name string - the only
lever is mutating the class object in place and hoping the pool inherits it,
which holds under ``fork`` and silently stops holding under ``spawn``.

So this chain gets its own converter. It is ~200 lines because the XML is flat,
and it fails loudly where the library returned an empty file.

Output matches what ``csvutil.read_rows`` expects: lower-cased tag names as
columns, and the literal ``''`` for a genuinely empty value (a bare empty cell
means "same as the row above" to that reader).
"""

import csv
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csvutil import EMPTY  # noqa: E402

# Explicit columns rather than "whatever tags showed up": the CSV needs a
# header before the first row is streamed out, and a stable header is what
# build_chain_db.py reads. Tags outside these lists are reported, not dropped
# in silence - see convert().
PRICE_COLUMNS = [
    "chainid", "subchainid", "storeid", "bikoretno",
    "priceupdatetime", "itemcode", "lastsaledatetime", "itemtype", "itemname",
    "manufacturename", "manufacturecountry", "manufactureitemdescription",
    "unitqty", "quantity", "unitofmeasure", "bisweighted", "qtyinpackage",
    "itemprice", "unitofmeasureprice", "allowdiscount", "itemstatus",
]

STORE_COLUMNS = [
    "chainid", "chainname", "lastupdatedate", "lastupdatetime",
    "subchainid", "subchainname",
    "storeid", "bikoretno", "storetype", "storename", "address", "city", "zipcode",
]

PROMO_COLUMNS = [
    "chainid", "subchainid", "storeid", "bikoretno",
    "promotionupdatetime", "allowmultiplediscounts", "promotionid",
    "promotiondescription", "promotionstartdatetime", "promotionenddatetime",
    "promotionstarthour", "promotionendhour", "promotiondays", "redemptionlimit",
    "minnoofitemoffered", "clubid", "isgiftitem", "additionaliscoupon",
    "additionalrestrictions", "remarks",
    "groupid", "minpurchaseamount", "discounttype",
    "itemcode", "itemtype", "rewardtype", "minqty", "maxqty",
    "discountrate", "discountedprice", "discountedpricepermida", "bisweighted",
]


def _tag(elem):
    return elem.tag.lower()


def _text(elem):
    return (elem.text or "").strip()


def _leaves(elem):
    """Direct leaf children of ``elem`` as {lowercased tag: text}."""
    return {_tag(child): _text(child) for child in elem if len(child) == 0}


def _iter_records(path, container_tag, record_tag):
    """Stream ``record_tag`` elements, each merged with the file's root fields.

    Root-level scalars (ChainID, StoreID, ...) close before the container
    opens, so they are complete by the time the first record arrives.
    """
    roots = {}
    container = None
    depth = 0
    for event, elem in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            depth += 1
            if _tag(elem) == container_tag:
                container = elem
            continue
        depth -= 1
        if depth == 2 and _tag(elem) == record_tag:
            yield roots, elem
            elem.clear()
            if container is not None:
                # Records stay attached to their container after being
                # emitted; without this the whole file accumulates in memory.
                container.clear()
        elif depth == 1 and len(elem) == 0:
            roots[_tag(elem)] = _text(elem)


def price_rows(path):
    """One row per <Item>."""
    for roots, item in _iter_records(path, "items", "item"):
        row = dict(roots)
        row.update(_leaves(item))
        yield row


def promo_rows(path):
    """One row per (promotion x group x item).

    build_chain_db.py reads a barcode straight off the row when there is one,
    so flattening here avoids the nested-JSON path it otherwise has to walk.
    """
    for roots, promo in _iter_records(path, "promotions", "promotion"):
        base = dict(roots)
        base.update(_leaves(promo))
        for groups in promo.findall("Groups"):
            for group in groups.findall("Group"):
                group_row = dict(base)
                group_row.update(_leaves(group))
                for items in group.findall("PromotionItems"):
                    for item in items.findall("PromotionItem"):
                        row = dict(group_row)
                        row.update(_leaves(item))
                        yield row


def store_rows(path):
    """One row per <Store>, carrying its chain and sub-chain fields."""
    root = ET.parse(path).getroot()
    base = _leaves(root)
    for subchains in root.findall("SubChains"):
        for subchain in subchains.findall("SubChain"):
            sub_row = dict(base)
            sub_row.update(_leaves(subchain))
            for stores in subchain.findall("Stores"):
                for store in stores.findall("Store"):
                    row = dict(sub_row)
                    row.update(_leaves(store))
                    yield row


READERS = {
    "PRICE_FULL_FILE": ("PriceFull", PRICE_COLUMNS, price_rows),
    "PROMO_FULL_FILE": ("PromoFull", PROMO_COLUMNS, promo_rows),
    "STORE_FILE": ("Stores", STORE_COLUMNS, store_rows),
}


def _xml_files(dumps_dir, prefix):
    hits = []
    for root, _dirs, files in os.walk(dumps_dir):
        for name in files:
            if name.startswith(prefix) and name.endswith(".xml"):
                hits.append(os.path.join(root, name))
    return sorted(hits)


def convert(dumps_dir, outputs_dir, file_types):
    """Write one CSV per requested file type. Returns {file_type: row count}."""
    os.makedirs(outputs_dir, exist_ok=True)
    counts = {}

    for file_type in file_types:
        if file_type not in READERS:
            raise ValueError(f"no Super-Pharm reader for {file_type}")
        prefix, columns, reader = READERS[file_type]

        paths = _xml_files(dumps_dir, prefix)
        if not paths:
            print(f"[superpharm] no {prefix}*.xml found - skipping {file_type}")
            counts[file_type] = 0
            continue

        target = os.path.join(outputs_dir, f"{file_type.lower()}_super_pharm.csv")
        unknown = set()
        written = 0
        with open(target, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for path in paths:
                for row in reader(path):
                    unknown.update(set(row) - set(columns))
                    writer.writerow({c: row.get(c) or EMPTY for c in columns})
                    written += 1

        print(f"[superpharm] {os.path.basename(target)}: "
              f"{written:,} rows from {len(paths)} file(s)")
        if unknown:
            # Not fatal - the chain adding a tag should not stop the build -
            # but it must not pass unnoticed either.
            print(f"[superpharm] WARNING: {file_type} has tags no column covers: "
                  f"{', '.join(sorted(unknown))}", file=sys.stderr)
        counts[file_type] = written

    return counts
