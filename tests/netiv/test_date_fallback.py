"""Reaching back a day when the chain has not published today's files yet.

Netiv Hesed's portal shows one day at a time. Every other chain's serves a
rolling window - Super-Pharm keeps about three days - so a morning where
today's snapshot is late still lists yesterday's and nothing notices. Here,
between the midnight rollover and whenever the full set lands, the listing
holds nothing but hourly deltas.

On 9 September 2026 the chain published at 16:59 instead of its usual 05:25.
The 07:26 build found a page full of deltas, took none of them, and failed
three times; it was the chain's first night on the site, so there was no
artifact to carry forward either and it did not appear at all.

The fallback asks the portal for earlier days, which it will serve. What it
must not do is pass those files off as tonight's: the app's rule is that stale
prices are labelled, not hidden, and backfill_chains.py stamps every chain a
run built with `now`. So a scrape that reached back writes the day down, and
backfill prefers that note to the time of the run that fetched it.
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import netiv  # noqa: E402

TODAY = datetime.date(2026, 9, 9)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def listing(name):
    with open(os.path.join(FIXTURES, f"listing_{name}.html"), encoding="utf-8") as h:
        return h.read()


class FakePortal:
    """The real portal's three answers, replayed from carved pages.

    `days_with_files` get the page that carries a full snapshot. `blank_days`
    get the one with no links at all. Everything else gets the deltas-only
    page - the shape the 07:26 build was served, and the reason "no links" and
    "no snapshot" have to be told apart.
    """

    def __init__(self, days_with_files, blank_days=()):
        self.days = set(days_with_files)
        self.blank = set(blank_days)
        self.asked = []

    def get(self, url, params=None, timeout=None):
        day = TODAY
        if params and params.get("Date"):
            day = datetime.date.fromisoformat(params["Date"])
        self.asked.append(day)
        if day in self.days:
            body = listing("with_snapshot")
        elif day in self.blank:
            body = listing("empty")
        else:
            body = listing("deltas_only")
        return type("Response", (), {
            "text": body,
            "raise_for_status": lambda self: None,
        })()


class TestNewestDayWithFiles:

    def test_todays_files_are_used_and_no_date_is_asked_for(self):
        portal = FakePortal([TODAY])
        day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert day is None
        assert len(names) == 3   # PriceFull, PromoFull, Stores
        assert portal.asked == [TODAY]      # the default listing, unparameterised

    def test_it_falls_back_to_yesterday(self):
        yesterday = TODAY - datetime.timedelta(days=1)
        portal = FakePortal([yesterday])
        day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert day == yesterday
        assert len(names) == 3   # PriceFull, PromoFull, Stores

    def test_it_takes_the_newest_day_it_can_find(self):
        portal = FakePortal([TODAY - datetime.timedelta(days=1),
                             TODAY - datetime.timedelta(days=3)])
        day, _names = netiv.newest_day_with_files(portal, today=TODAY)
        assert day == TODAY - datetime.timedelta(days=1)

    def test_it_gives_up_rather_than_publishing_last_week(self):
        # Past the lookback the chain has a real problem, and the previous
        # night's artifact - carried forward and labelled - is no worse.
        portal = FakePortal([TODAY - datetime.timedelta(days=6)])
        day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert (day, names) == (None, [])

    def test_a_day_the_chain_skipped_does_not_stop_the_walk(self):
        # A blank day returns a page with no links at all, which on the
        # default listing means Cloudflare got in the way. On a date it means
        # the chain published nothing, and the walk has to keep going.
        portal = FakePortal([TODAY - datetime.timedelta(days=2)],
                            blank_days=[TODAY - datetime.timedelta(days=1)])
        day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert day == TODAY - datetime.timedelta(days=2)
        assert len(names) == 3   # PriceFull, PromoFull, Stores

    def test_a_blank_today_does_not_stop_the_walk_either(self):
        # Right after the midnight rollover even the deltas can be missing.
        # That looks exactly like a challenge page, so it is not called one
        # until every day has failed to answer.
        portal = FakePortal([TODAY - datetime.timedelta(days=1)],
                            blank_days=[TODAY])
        day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert day == TODAY - datetime.timedelta(days=1)
        assert len(names) == 3   # PriceFull, PromoFull, Stores

    def test_no_listing_on_any_day_is_an_error(self):
        # Not "the chain published nothing all week" - that is what a page
        # with links and no snapshots says. This is not being served at all.
        portal = FakePortal([], blank_days=[TODAY - datetime.timedelta(days=n)
                                            for n in range(0, 5)])
        try:
            netiv.newest_day_with_files(portal, today=TODAY)
        except RuntimeError:
            return
        raise AssertionError("a portal that never lists anything must raise")

    def test_a_week_of_deltas_is_not_an_error_just_empty(self):
        # The portal is answering, the chain simply has not published. Let the
        # caller fail on "no XML" and the artifact carry the chain forward.
        portal = FakePortal([])
        assert netiv.newest_day_with_files(portal, today=TODAY) == (None, [])

    def test_the_deltas_are_never_taken(self):
        portal = FakePortal([])
        _day, names = netiv.newest_day_with_files(portal, today=TODAY)
        assert names == []


class TestRecordAsOf:
    """The note that stops old prices being published as tonight's."""

    def as_of(self, tmp_path, published_on):
        netiv.record_as_of(published_on, str(tmp_path))
        path = tmp_path / "_asof_NETIV_HASED.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_nothing_is_written_when_the_files_are_todays(self, tmp_path):
        assert self.as_of(tmp_path, None) is None

    def test_the_day_is_written_when_the_scrape_reached_back(self, tmp_path):
        noted = self.as_of(tmp_path, datetime.date(2026, 9, 8))
        assert noted == {"NETIV_HASED": "2026-09-08T12:00:00+00:00"}
