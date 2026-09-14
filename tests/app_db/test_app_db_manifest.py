"""app_db_manifest.py against a real app.db built by build_app_db.build().

swap-app-db.sh (KAN-11) trusts this manifest completely - a wrong sha256 or
row count here is a wrong swap decision on the VM - so the fields it reads
have to come from the built file itself, never be guessed or copied from the
input prices.db.
"""

import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import app_db_manifest  # noqa: E402
import build_app_db  # noqa: E402

# Reuse KAN-6's fixture builder for a small, real prices.db.
from test_build_app_db import fixture_db  # noqa: E402,F401


def test_manifest_matches_the_built_file(fixture_db, tmp_path):
    db_path, _, _ = fixture_db
    out_path = str(tmp_path / "app.db")
    build_app_db.build(db_path, out_path)

    manifest = app_db_manifest.build_manifest(out_path)

    with open(out_path, "rb") as fh:
        assert manifest["sha256"] == hashlib.sha256(fh.read()).hexdigest()
    assert manifest["size_bytes"] == os.path.getsize(out_path)

    conn = sqlite3.connect(out_path)
    try:
        (expected_built_at,) = conn.execute(
            "SELECT value FROM meta WHERE key='built_at'"
        ).fetchone()
        for table in ("products", "chain_prices", "deals"):
            (expected,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert manifest["rows"][table] == expected
    finally:
        conn.close()
    assert manifest["built_at"] == expected_built_at


def test_manifest_is_written_as_json(tmp_path):
    db_path = str(tmp_path / "fixture.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE products(barcode TEXT);"
        "CREATE TABLE chain_prices(barcode TEXT);"
        "CREATE TABLE deals(id INTEGER);"
    )
    conn.execute("INSERT INTO meta VALUES ('built_at', '2026-09-14T00:00:00+00:00')")
    conn.executemany("INSERT INTO products VALUES (?)", [("1",), ("2",)])
    conn.commit()
    conn.close()

    out_path = str(tmp_path / "app.db.json")
    import subprocess

    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "app_db_manifest.py"),
         "--db", db_path, "--out", out_path],
        check=True, capture_output=True, text=True,
    )
    with open(out_path) as fh:
        manifest = json.load(fh)
    assert manifest["built_at"] == "2026-09-14T00:00:00+00:00"
    assert manifest["rows"] == {"products": 2, "chain_prices": 0, "deals": 0}


def test_missing_built_at_is_rejected(tmp_path):
    db_path = str(tmp_path / "no_meta.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE products(barcode TEXT);"
        "CREATE TABLE chain_prices(barcode TEXT);"
        "CREATE TABLE deals(id INTEGER);"
    )
    conn.commit()
    conn.close()

    try:
        app_db_manifest.build_manifest(db_path)
    except SystemExit as exc:
        assert "built_at" in str(exc)
    else:
        raise AssertionError("expected SystemExit for missing built_at")
