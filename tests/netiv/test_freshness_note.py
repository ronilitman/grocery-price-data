"""backfill_chains.py must believe the note over the clock.

It stamps every chain a run built with *now*, which is what makes a scrape
count as fresh. A chain whose portal had not published today's files yet is
still "built by this run" - the databases are real and tonight's - but the
prices in them are not, and stamping those with now is how yesterday's numbers
reach a shopper with nothing marking them.

So a chain that reached back leaves `_asof_<CHAIN>.json` beside its database,
and it wins. A note for a chain that failed anyway does not: that chain's rows
come from an artifact, whose own age is already the honest answer.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import backfill_chains  # noqa: E402

YESTERDAY = "2026-09-08T12:00:00+00:00"


def run(tmp_path, monkeypatch, built, notes):
    """Run backfill over a directory holding `built` databases and `notes`."""
    for chain in built:
        sqlite3.connect(str(tmp_path / f"{chain.lower()}.db")).close()
    for chain, as_of in notes.items():
        (tmp_path / f"_asof_{chain}.json").write_text(json.dumps({chain: as_of}))

    # No artifact listing: this is about the stamp, not about carrying forward,
    # and the real one shells out to `gh`.
    monkeypatch.setattr(backfill_chains, "artifact_index", lambda repo: {})
    monkeypatch.setattr(sys, "argv",
                        ["backfill_chains.py", "--dir", str(tmp_path), "--repo", "x/y"])
    backfill_chains.main()
    return json.loads((tmp_path / "_freshness.json").read_text())


def test_a_chain_with_no_note_is_stamped_now(tmp_path, monkeypatch):
    freshness = run(tmp_path, monkeypatch, built=["NETIV_HASED"], notes={})
    assert freshness["NETIV_HASED"] > YESTERDAY


def test_the_note_replaces_the_run_time(tmp_path, monkeypatch):
    freshness = run(tmp_path, monkeypatch, built=["NETIV_HASED"],
                    notes={"NETIV_HASED": YESTERDAY})
    assert freshness["NETIV_HASED"] == YESTERDAY


def test_one_chains_note_leaves_the_others_alone(tmp_path, monkeypatch):
    freshness = run(tmp_path, monkeypatch, built=["NETIV_HASED", "RAMI_LEVY"],
                    notes={"NETIV_HASED": YESTERDAY})
    assert freshness["NETIV_HASED"] == YESTERDAY
    assert freshness["RAMI_LEVY"] > YESTERDAY


def test_a_note_for_a_chain_that_did_not_build_is_ignored(tmp_path, monkeypatch):
    # The scrape fell back and then failed anyway. Whatever rows reach the
    # site come from an artifact, and its age is the one to report.
    freshness = run(tmp_path, monkeypatch, built=[], notes={"NETIV_HASED": YESTERDAY})
    assert "NETIV_HASED" not in freshness
