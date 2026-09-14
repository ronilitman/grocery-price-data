"""Write the manifest swap-app-db.sh checks before swapping in app.db (KAN-11).

Usage:
    python3 scripts/app_db_manifest.py --db app.db --out app.db.json

Fields: built_at (from the db's own meta table, so the manifest can never
disagree with the file it describes), sha256 of the *uncompressed* app.db
(compression is not deterministic across zstd versions/flags, so the sha
is taken before compressing and checked after decompressing on the VM),
byte size of the uncompressed file, and row counts for products,
chain_prices and deals - the same three tables swap-app-db.sh and a human
reading the CI job summary care about.
"""

import argparse
import hashlib
import json
import sqlite3


def build_manifest(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        built_at = meta.get("built_at")
        if not built_at:
            raise SystemExit(f"{db_path}: meta.built_at is missing/empty")
        rows = {}
        for table in ("products", "chain_prices", "deals"):
            (rows[table],) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        conn.close()

    sha256 = hashlib.sha256()
    size = 0
    with open(db_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha256.update(chunk)
            size += len(chunk)

    return {
        "built_at": built_at,
        "sha256": sha256.hexdigest(),
        "size_bytes": size,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="app.db to describe (uncompressed)")
    parser.add_argument("--out", required=True, help="manifest path to write, e.g. app.db.json")
    args = parser.parse_args()

    manifest = build_manifest(args.db)
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"[app_db_manifest] {args.out}: {json.dumps(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
