#!/usr/bin/env python3
"""Offline tests for the fare watch. Stdlib only: python3 test_farewatch.py

These drive main() against stubbed endpoints in a temp directory, so they
exercise the path the daily run actually takes, feed entry and all.
"""

import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile

import farewatch

KEY_1A = "1A+1+A+B"
KEY_106 = "106+1+A+B"


def route(co, name, bound, codes):
    return {"co": co, "route": name, "bound": {"kmb": bound}, "serviceType": 1,
            "stops": {"kmb": codes}, "fares": ["5.0"] * (len(codes) - 1)}


DB = {
    "stopList": {c: {"name": {"zh": "站 (%s)" % c}}
                 for c in ("S1", "S2", "S3", "J1", "J2", "J3")},
    "routeList": {
        KEY_1A: route(["kmb"], "1A", "O", ["S1", "S2", "S3"]),
        KEY_106: route(["kmb", "ctb"], "106", "I", ["J1", "J2", "J3"]),
    },
}

# Both routes: KMB prices the second stop at 6.0 where the feed says 5.0.
DIVERGING = ("5.0", "6.0", "0")
AGREEING = ("5.0", "5.0", "0")
UNPRICED = ("5.0", "0", "0")      # KMB stops publishing a fare for that stop
CODES = {"106": ["J1", "J2", "J3"], "1A": ["S1", "S2", "S3"]}


def stub(db, fail=(), short=(), fares=None):
    fares = fares or {}

    def fake_get(url, tries=3):
        if url == farewatch.DB_URL:
            return db
        name = "106" if "route=106" in url else "1A"
        if name in fail:
            raise RuntimeError("stubbed network failure")
        got = [{"CName": "站 (%s)" % c, "AirFare": f}
               for f, c in zip(fares.get(name, DIVERGING), CODES[name])]
        if name in short:
            got = got[:-1]                      # stop count no longer matches
        return {"data": {"routeStops": got}}
    return fake_get


def newest(feed):
    """Title of the first <entry> in a feed.xml, or None if it has none."""
    m = re.search(r"<entry>.*?<title>(.*?)</title>.*?</entry>", feed, re.S)
    return m.group(1) if m else None


def run(old_state=None, db=None, **kw):
    """One full main() run in a temp dir.

    Returns (state, title of any entry this run published). The feed caps at 50
    entries, so growth in length cannot be used to spot an insertion.
    """
    was = (old_state["entries"] or [None])[0] if old_state else None
    saved = (farewatch.get, farewatch.HERE)
    farewatch.get = stub(db if db is not None else DB, **kw)
    try:
        with tempfile.TemporaryDirectory() as d:
            if old_state is not None:
                json.dump(old_state, open(os.path.join(d, "state.json"), "w"))
            farewatch.HERE = d
            with contextlib.redirect_stdout(io.StringIO()):
                farewatch.main()
            state = json.load(open(os.path.join(d, "state.json")))
            feed = open(os.path.join(d, "feed.xml"), encoding="utf-8").read()
    finally:
        farewatch.get, farewatch.HERE = saved
    top = (state["entries"] or [None])[0]
    published = newest(feed) if top is not None and top != was else None
    assert (newest(feed) is None) == (not state["entries"]), "feed lost entries"
    return state, published


def seeded():
    """A previous run that recorded a divergence on each route."""
    state, _ = run()
    assert len(state["divergences"]) == 2, state["divergences"]
    return copy.deepcopy(state)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def joint_routes_are_compared():
    """A route KMB shares with Citybus must not be dropped before comparison."""
    state, _ = run()
    routes = sorted({d["route"] for d in state["divergences"].values()})
    assert state["coverage"]["targets"] == 2, state["coverage"]
    assert routes == ["106", "1A"], routes


def assert_silent(title, state, why):
    assert title is None, "%s but the feed published %r" % (why, title)
    assert len(state["divergences"]) == 2, (why, state["divergences"])


@case
def a_failed_fetch_does_not_read_as_agreement():
    state, title = run(seeded(), fail=("106",))
    assert state["coverage"]["failed"] == 1, state["coverage"]
    assert_silent(title, state, "the request failed")


@case
def a_newly_skipped_route_does_not_read_as_agreement():
    state, title = run(seeded(), short=("106",))
    assert state["coverage"]["skipped"] == {"stop count": 1}, state["coverage"]
    assert_silent(title, state, "the route was skipped")


@case
def a_route_that_loses_its_fares_does_not_read_as_agreement():
    """Dropped before check() runs, so nothing ever looked at those stops."""
    db = copy.deepcopy(DB)
    db["routeList"][KEY_106]["fares"] = []
    state, title = run(seeded(), db=db)
    assert state["coverage"]["targets"] == 1, state["coverage"]
    assert_silent(title, state, "the route lost its fares")


@case
def a_stop_kmb_stops_pricing_does_not_read_as_agreement():
    """AirFare 0 is skipped inside an otherwise successful comparison."""
    state, title = run(seeded(), fares={"106": UNPRICED})
    assert state["coverage"]["compared"] == 2, state["coverage"]
    assert_silent(title, state, "KMB published no fare for that stop")


@case
def an_unusable_fare_does_not_read_as_agreement():
    """float("NaN") parses fine and compares false against everything."""
    state, title = run(seeded(), fares={"106": ("5.0", "NaN", "0")})
    assert state["coverage"]["compared"] == 2, state["coverage"]
    assert_silent(title, state, "the fare was not a usable number")


@case
def a_resolution_is_published_even_with_a_full_feed():
    """entries is capped at 50, so length cannot detect an insertion."""
    old = seeded()
    old["entries"] = old["entries"] * 50
    state, title = run(old, fares={"106": AGREEING})
    assert len(state["entries"]) == 50, len(state["entries"])
    assert title == "Fare divergence: 1 resolved", title


@case
def a_route_that_now_agrees_still_resolves():
    """Carrying findings forward must not make a real resolution invisible."""
    state, title = run(seeded(), fares={"106": AGREEING})
    assert title == "Fare divergence: 1 resolved", title
    assert len(state["divergences"]) == 1, state["divergences"]


@case
def a_route_that_leaves_the_feed_still_resolves():
    """A route no longer published is gone, not unobserved."""
    db = copy.deepcopy(DB)
    del db["routeList"][KEY_106]
    state, title = run(seeded(), db=db)
    assert title == "Fare divergence: 1 resolved", title
    assert len(state["divergences"]) == 1, state["divergences"]


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
