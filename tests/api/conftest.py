import json
import sqlite3
from pathlib import Path

import pytest

FIXTURE_BUILT_AT = "2026-09-13T02:00:00Z"
FIXTURE_CHAIN_AS_OF = {"NETIV_HASED": "2026-09-11"}
FIXTURE_PRODUCTS = [
    ("7290000000001", "חלב 3%"),
    ("7290000000002", "לחם אחיד"),
    ("7290000000003", "ביצים L"),
]


def _build_fixture_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta (built_at TEXT, chain_as_of TEXT)")
        conn.execute(
            "INSERT INTO meta (built_at, chain_as_of) VALUES (?, ?)",
            (FIXTURE_BUILT_AT, json.dumps(FIXTURE_CHAIN_AS_OF)),
        )
        conn.execute("CREATE TABLE products (barcode TEXT PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO products (barcode, name) VALUES (?, ?)", FIXTURE_PRODUCTS
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def fixture_db(tmp_path) -> Path:
    db_path = tmp_path / "fixture.db"
    _build_fixture_db(db_path)
    return db_path
