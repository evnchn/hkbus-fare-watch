#!/usr/bin/env python3
"""Tests for what subscribers receive. Stdlib only: python3 test_feed.py"""

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile

import farewatch

N = 27                                   # enough stops to overflow the 25 cap
KEY = "X1+1+A+B"
CODES = ["S%d" % i for i in range(N)]
DB = {
    "stopList": {c: {"name": {"zh": "站 (%s)" % c}} for c in CODES},
    "routeList": {KEY: {"co": ["kmb"], "route": "X1", "bound": {"kmb": "O"},
                        "serviceType": 1, "stops": {"kmb": CODES},
                        "fares": ["5.0"] * (N - 1)}},
}


def fake_get(url, tries=3):
    if url == farewatch.DB_URL:
        return DB
    # KMB prices every stop at 6.0 where the feed says 5.0
    return {"data": {"routeStops": [{"CName": "站 (%s)" % c, "AirFare": "6.0"}
                                    for c in CODES]}}


class Clock:
    """Successive runs must get distinct stamps, or a quiet day looks stable."""

    def __init__(self):
        self.n = 0

    def now(self, tz=None):
        self.n += 1
        return dt.datetime(2026, 1, 1, 0, 0, self.n, tzinfo=dt.timezone.utc)


def run(d, clock):
    saved = (farewatch.get, farewatch.HERE, farewatch.datetime)
    farewatch.get, farewatch.HERE, farewatch.datetime = fake_get, d, clock
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            farewatch.main()
        return open(os.path.join(d, "feed.xml"), encoding="utf-8").read()
    finally:
        farewatch.get, farewatch.HERE, farewatch.datetime = saved


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def the_feed_declares_an_author():
    """RFC 4287 4.1.1: a feed MUST carry an author unless every entry does."""
    with tempfile.TemporaryDirectory() as d:
        feed = run(d, Clock())
    assert "<author>" in feed, feed[:400]
    assert "</author>" in feed.split("<entry>")[0], "author must be feed-level"


@case
def a_quiet_day_leaves_the_feed_untouched():
    """Nothing changed, so nothing about the feed should say it did."""
    clock = Clock()
    with tempfile.TemporaryDirectory() as d:
        first = run(d, clock)
        second = run(d, clock)
        stamps = json.load(open(os.path.join(d, "state.json")))["updated"]
    assert stamps.endswith(":02Z"), stamps          # the run really did happen
    def updated(feed):
        return [l for l in feed.split("\n") if "<updated>" in l][0].strip()

    assert first == second, "quiet day rewrote the feed: %s -> %s" % (
        updated(first), updated(second))


@case
def a_truncated_list_points_at_the_standing_list():
    """26 findings, 25 shown; the rest must be reachable."""
    with tempfile.TemporaryDirectory() as d:
        feed = run(d, Clock())
    assert "and 1 more" in feed, feed[-700:]
    assert farewatch.REPORT_URL.replace("&", "&amp;") in feed, "no link to the report"


def main():
    bad = 0
    for fn in CASES:
        try:
            fn()
        except AssertionError as e:
            bad += 1
            print("FAIL: %s\n      %s" % (fn.__name__, e))
        except Exception as e:
            bad += 1
            print("ERROR: %s\n      %r" % (fn.__name__, e))
        else:
            print("ok:   %s" % fn.__name__)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
