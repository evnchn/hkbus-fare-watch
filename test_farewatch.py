#!/usr/bin/env python3
"""Offline tests for the fare watch. Stdlib only: python3 test_farewatch.py

These drive main() against stubbed endpoints in a temp directory, so they
exercise the path the daily run actually takes, feed entry and all.
"""

import contextlib
import copy
import html
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


def stub(db, fail=(), short=(), recode=(), fares=None, bare=(), codes=None):
    fares = fares or {}
    stops = dict(CODES, **(codes or {}))

    def fake_get(url, tries=3):
        if url == farewatch.DB_URL:
            return db
        name = "106" if "route=106" in url else "1A"
        if name in fail:
            raise RuntimeError("stubbed network failure")
        names = stops[name]
        if name in recode:                      # KMB now names different stops
            names = ["X%d" % i for i in range(len(names))]
        label = "站" if name in bare else "站 (%s)"     # bare: KMB names no code
        got = [{"CName": label.replace("%s", c), "AirFare": f}
               for f, c in zip(fares.get(name, DIVERGING), names)]
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


def assert_no_false_resolution(title, state, why):
    """A finding nobody looked at is reported as unverified, and kept.

    Asserting only "not resolved" is too weak: it also accepts announcing the
    carried finding as new, or changed, or saying nothing at all. Pin the exact
    title so a rendering mistake cannot pass by being merely non-committal.
    """
    assert title == "Fare divergence: 1 not checked", (
        "%s but the feed published %r" % (why, title))
    assert len(state["divergences"]) == 2, (why, state["divergences"])
    body = state["entries"][0]
    assert "not verified this run" in html.unescape(body), (why, body[:300])


@case
def a_failed_fetch_does_not_read_as_agreement():
    state, title = run(seeded(), fail=("106",))
    assert state["coverage"]["failed"] == 1, state["coverage"]
    assert_no_false_resolution(title, state, "the request failed")


@case
def a_newly_skipped_route_does_not_read_as_agreement():
    state, title = run(seeded(), short=("106",))
    assert state["coverage"]["skipped"] == {"stop count": 1}, state["coverage"]
    assert_no_false_resolution(title, state, "the route was skipped")


@case
def a_stop_code_mismatch_does_not_read_as_agreement():
    """The 2026-10-07 incident: the stop-code guard fired, eight route-directions
    left the compared set, and findings on them were published as resolved. Two
    came back two days later with identical values."""
    state, title = run(seeded(), recode=("106",))
    assert state["coverage"]["skipped"] == {"stop codes": 1}, state["coverage"]
    assert_no_false_resolution(title, state, "the stop codes disagreed")


@case
def a_route_that_loses_its_fares_does_not_read_as_agreement():
    """Dropped before check() runs, so nothing ever looked at those stops."""
    db = copy.deepcopy(DB)
    db["routeList"][KEY_106]["fares"] = []
    state, title = run(seeded(), db=db)
    assert state["coverage"]["targets"] == 1, state["coverage"]
    assert_no_false_resolution(title, state, "the route lost its fares")


@case
def a_stop_kmb_stops_pricing_does_not_read_as_agreement():
    """AirFare 0 is skipped inside an otherwise successful comparison."""
    state, title = run(seeded(), fares={"106": UNPRICED})
    assert state["coverage"]["compared"] == 2, state["coverage"]
    assert_no_false_resolution(title, state, "KMB published no fare for that stop")


@case
def an_unusable_fare_does_not_read_as_agreement():
    """float("NaN") parses fine and compares false against everything."""
    state, title = run(seeded(), fares={"106": ("5.0", "NaN", "0")})
    assert state["coverage"]["compared"] == 2, state["coverage"]
    assert_no_false_resolution(title, state, "the fare was not a usable number")


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


@case
def a_route_with_no_stop_codes_is_skipped_not_compared_blind():
    """Issue 3: if neither side names its stops with a code, the stop-code guard
    has checked nothing, so the route must not count as verified."""
    db = copy.deepcopy(DB)
    for c in CODES["106"]:
        db["stopList"][c]["name"]["zh"] = "站%s" % c
    state, _ = run(db=db, bare=("106",))
    assert state["coverage"]["skipped"] == {"no stop codes": 1}, state["coverage"]
    assert {d["route"] for d in state["divergences"].values()} == {"1A"}, (
        state["divergences"])


def as_index_keyed(state):
    """The same state as a run before findings were keyed by stop wrote it."""
    old = {k: "%s|%d" % (k.split("|")[0], d["seq"])
           for k, d in state["divergences"].items()}
    state["divergences"] = {old[k]: d for k, d in state["divergences"].items()}
    state["unchecked"] = sorted(old[k] for k in state["unchecked"])
    return state


def with_stops(db, key, stops):
    db = copy.deepcopy(db)
    db["stopList"].update({c: {"name": {"zh": "站 (%s)" % c}} for c in stops})
    db["routeList"][key]["stops"]["kmb"] = stops
    return db


@case
def a_stop_replaced_at_the_same_index_is_not_read_as_the_old_one():
    """Issue 5: J2 becomes J4 in both sources, with the same diverging fare. A
    positional key would carry J2's finding over to J4 and publish nothing."""
    db = with_stops(DB, KEY_106, ["J1", "J4", "J3"])
    state, title = run(seeded(), db=db, codes={"106": ["J1", "J4", "J3"]})
    assert title == "Fare divergence: 1 new, 1 resolved", title
    assert KEY_106 + "|J4" in state["divergences"], list(state["divergences"])
    body = html.unescape(state["entries"][0])
    assert "J2): was $5.0 against KMB's $6.0, no longer on the route" in body, body


@case
def a_state_keyed_by_index_settles_without_announcing_anything():
    """Deploy day: every stored key changes shape, and nothing else does."""
    old = as_index_keyed(seeded())
    state, title = run(old)
    assert title is None, "re-keying the state published %r" % title
    assert sorted(state["divergences"]) == [KEY_106 + "|J2", KEY_1A + "|S2"], (
        list(state["divergences"]))

    carrying, _ = run(seeded(), fail=("106",))
    state, title = run(as_index_keyed(carrying), fail=("106",))
    assert title is None, "re-keying the unchecked list published %r" % title


@case
def a_stop_served_twice_keeps_each_visit_apart():
    """A loop route passes L1 twice. One visit agreeing must not hand its key to
    the other, and a failed fetch must carry the second visit, not resolve it."""
    loop = ["L1", "L2", "L1", "L3"]
    db = with_stops(DB, KEY_106, loop)
    db["routeList"][KEY_106]["fares"] = ["5.0"] * 3
    kw = {"db": db, "codes": {"106": loop}}
    both, _ = run(fares={"106": ("6.0", "5.0", "6.0", "0")}, **kw)
    assert {KEY_106 + "|L1", KEY_106 + "|L1|1"} <= set(both["divergences"]), (
        list(both["divergences"]))

    state, title = run(copy.deepcopy(both), fares={"106": ("5.0", "5.0", "6.0", "0")}, **kw)
    assert title == "Fare divergence: 1 resolved", title
    assert KEY_106 + "|L1|1" in state["divergences"], list(state["divergences"])

    state, title = run(copy.deepcopy(both), fail=("106",), **kw)
    assert title == "Fare divergence: 2 not checked", title

    # A stop added before the second visit must not make it a different finding.
    longer = ["L1", "L2", "L4", "L1", "L3"]
    db2 = with_stops(db, KEY_106, longer)
    db2["routeList"][KEY_106]["fares"] = ["5.0"] * 4
    state, title = run(copy.deepcopy(both), db=db2, codes={"106": longer},
                       fares={"106": ("6.0", "5.0", "5.0", "6.0", "0")})
    assert title is None, "a stop inserted before the loop's second visit published %r" % title


@case
def a_loop_visit_keyed_by_index_migrates_to_the_same_visit():
    """Only the second visit to L1 diverges. Migrating its old index key must
    land on the second visit, not the first, or deploy day reports a change."""
    loop = ["L1", "L2", "L1", "L3"]
    db = with_stops(DB, KEY_106, loop)
    db["routeList"][KEY_106]["fares"] = ["5.0"] * 3
    kw = {"db": db, "codes": {"106": loop}, "fares": {"106": ("5.0", "5.0", "6.0", "0")}}
    old, _ = run(**kw)
    state, title = run(as_index_keyed(copy.deepcopy(old)), **kw)
    assert title is None, "migrating a second visit published %r" % title
    assert KEY_106 + "|L1|1" in state["divergences"], list(state["divergences"])

    # Deploy day's fetch fails: the old key waits for a run that saw the route.
    waited, _ = run(as_index_keyed(copy.deepcopy(old)), fail=("106",), **kw)
    state, title = run(waited, **dict(kw, fares={"106": ("5.0", "5.0", "0", "0")}))
    assert KEY_106 + "|L1|1" in state["divergences"], list(state["divergences"])
    assert title != "Fare divergence: 1 resolved", "an unpriced second visit resolved"

    # The loop gains a stop before its first visit on deploy day, so the old
    # index cannot say which L1 it was: it must wait, not resolve.
    longer = ["L4", "L1", "L2", "L1", "L3"]
    db2 = with_stops(db, KEY_106, longer)
    db2["routeList"][KEY_106]["fares"] = ["5.0"] * 4
    first = dict(kw, fares={"106": ("6.0", "5.0", "5.0", "0")})
    old, _ = run(**first)
    state, title = run(as_index_keyed(copy.deepcopy(old)), db=db2, codes={"106": longer},
                       fares={"106": ("5.0", "0", "5.0", "0", "0")})
    assert KEY_106 + "|0" in state["divergences"], list(state["divergences"])
    assert "resolved" not in (title or ""), "an ambiguous legacy key resolved: %r" % title

    # Same reshape, but every L1 visit is priced and agrees: it has resolved.
    state, title = run(as_index_keyed(copy.deepcopy(old)), db=db2, codes={"106": longer},
                       fares={"106": ("5.0", "5.0", "5.0", "5.0", "0")})
    assert KEY_106 + "|0" not in state["divergences"], list(state["divergences"])
    assert title == "Fare divergence: 1 resolved", title
    assert "now agrees" in state["entries"][0], state["entries"][0]

    # One visit agrees but the other is unpriced, or still diverges: not resolved.
    for fares in (("5.0", "5.0", "5.0", "0", "0"), ("5.0", "5.0", "5.0", "6.0", "0")):
        state, title = run(as_index_keyed(copy.deepcopy(old)), db=db2, codes={"106": longer},
                           fares={"106": fares})
        assert KEY_106 + "|0" in state["divergences"], (fares, list(state["divergences"]))
        assert "resolved" not in (title or ""), (fares, title)


@case
def the_standing_list_tells_service_types_apart():
    """Two service types of one route and bound must not read as duplicate rows."""
    db = copy.deepcopy(DB)
    db["routeList"]["1A+2+A+B"] = dict(db["routeList"][KEY_1A], serviceType="2")
    del db["routeList"][KEY_1A]["serviceType"]   # absent upstream: the default
    saved = (farewatch.get, farewatch.HERE)
    farewatch.get = stub(db, fares={"1A": ("6.0", "6.0", "0")})
    try:
        with tempfile.TemporaryDirectory() as d:
            farewatch.HERE = d
            with contextlib.redirect_stdout(io.StringIO()):
                farewatch.main()
            rows = [r for r in open(os.path.join(d, "report.md"), encoding="utf-8")
                    if r.startswith("| 1A")]
            feed = html.unescape(open(os.path.join(d, "feed.xml"), encoding="utf-8").read())
    finally:
        farewatch.get, farewatch.HERE = saved
    labels = [r.split(" | ")[0] for r in rows]
    assert labels == ["| 1A"] * 2 + ["| 1A (service type 2)"] * 2, rows
    assert len(set(rows)) == len(rows), rows
    listed = re.findall(r"<li>(1A [^:]*):", feed)
    assert listed == ["1A outbound at 站 (S1)", "1A outbound at 站 (S2)",
                      "1A outbound (service type 2) at 站 (S1)",
                      "1A outbound (service type 2) at 站 (S2)"], listed


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
