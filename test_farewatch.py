#!/usr/bin/env python3
"""Offline tests for the sweep. Stdlib only: python3 test_farewatch.py"""

import contextlib
import io
import json
import os
import sys
import tempfile

import farewatch


def stop(code):
    return {"name": {"zh": "站 (%s)" % code}}


def route(co, name, bound, codes):
    return {"co": co, "route": name, "bound": {"kmb": bound}, "serviceType": 1,
            "stops": {"kmb": codes}, "fares": ["5.0"] * (len(codes) - 1)}


DB = {
    "stopList": {c: stop(c) for c in ("S1", "S2", "S3", "J1", "J2", "J3")},
    "routeList": {
        "1A+1+A+B": route(["kmb"], "1A", "O", ["S1", "S2", "S3"]),
        "106+1+A+B": route(["kmb", "ctb"], "106", "I", ["J1", "J2", "J3"]),
    },
}

# Both routes have a second-stop fare KMB puts at 6.0 and the feed at 5.0.
KMB_ROWS = [{"CName": "站 (%s)" % c, "AirFare": f}
            for c, f in (("x1", "5.0"), ("x2", "6.0"), ("x3", "0"))]


def fake_get(url, tries=3):
    if url == farewatch.DB_URL:
        return DB
    codes = ["J1", "J2", "J3"] if "route=106" in url else ["S1", "S2", "S3"]
    return {"data": {"routeStops": [dict(r, CName="站 (%s)" % c)
                                    for r, c in zip(KMB_ROWS, codes)]}}


def stub(db, codes):
    """KMB answers every route with KMB_ROWS, named by codes[route]."""
    def get(url, tries=3):
        if url == farewatch.DB_URL:
            return db
        name = url.split("route=")[1].split("&")[0]
        return {"data": {"routeStops": [dict(r, CName="站 (%s)" % c)
                                        for r, c in zip(KMB_ROWS, codes[name])]}}
    return get


def run(get, state=None):
    """One main() in a temp dir; returns (state, published an entry?)."""
    saved = farewatch.get, farewatch.HERE
    with tempfile.TemporaryDirectory() as d:
        if state is not None:
            json.dump(state, open(os.path.join(d, "state.json"), "w"))
        farewatch.get, farewatch.HERE = get, d
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                farewatch.main()
        finally:
            farewatch.get, farewatch.HERE = saved
        new = json.load(open(os.path.join(d, "state.json")))
    return new, state is not None and new["entries"] != state["entries"]


def main():
    farewatch.get = fake_get
    divergences, coverage = farewatch.sweep()
    routes = sorted({d["route"] for d in divergences.values()})

    failures = []
    if "1A" not in routes:
        failures.append("control: a kmb-only divergence was not reported at all")
    if "106" not in routes:
        failures.append(
            "a joint kmb+ctb route was never compared, so its divergence is "
            "invisible; reported routes were %r" % routes)
    if coverage["targets"] != 2:
        failures.append("expected 2 targets, got %d" % coverage["targets"])

    # Issue 3: neither side names its stops with a code, so nothing checks the
    # stop order; the route must be skipped, not compared on stop count alone.
    bare = {"stopList": {c: {"name": {"zh": "站%s" % c}} for c in ("B1", "B2", "B3")},
            "routeList": {"9+1+A+B": route(["kmb"], "9", "O", ["B1", "B2", "B3"])}}
    farewatch.get = lambda url, tries=3: bare if url == farewatch.DB_URL else {
        "data": {"routeStops": [dict(r, CName="別站") for r in KMB_ROWS]}}
    found, covered = farewatch.sweep()
    if found or covered["skipped"] != {"no stop codes": 1}:
        failures.append("a route with no stop codes on either side passed the "
                        "stop-code guard unchecked: %r, %r" % (found, covered))

    # Issue 5: J2 is replaced by J4 at the same index in both sources, and J4
    # diverges by the same amounts. That is a different stop, not J2 unchanged.
    codes = {"1A": ["S1", "S2", "S3"], "106": ["J1", "J2", "J3"]}
    seeded, _ = run(stub(DB, codes))
    swapped = json.loads(json.dumps(DB))
    swapped["stopList"]["J4"] = stop("J4")
    swapped["routeList"]["106+1+A+B"]["stops"]["kmb"] = ["J1", "J4", "J3"]
    state, published = run(stub(swapped, dict(codes, **{"106": ["J1", "J4", "J3"]})),
                           seeded)
    if not published or "106+1+A+B|J4" not in state["divergences"]:
        failures.append("a stop replaced at the same index inherited the old "
                        "stop's finding silently: published=%r, keys=%r"
                        % (published, sorted(state["divergences"])))

    # The same two findings as state.json held them before issue 5, keyed by
    # index. Nothing has changed, so the re-keying must not publish anything.
    old = {"entries": ["  <entry>earlier</entry>\n"], "divergences": {
        "%s|1" % k: {"route": n, "bound": b, "serviceType": 1, "seq": 1,
                     "stop": c, "stopName": "站 (%s)" % c, "app": 5.0, "kmb": 6.0}
        for k, n, b, c in (("1A+1+A+B", "1A", "O", "S2"),
                           ("106+1+A+B", "106", "I", "J2"))}}
    state, published = run(stub(DB, codes), old)
    if published:
        failures.append("re-keying state.json published unchanged findings as "
                        "resolved and new: %r" % state["entries"][0])

    # A loop route serves L1 twice. When the first L1 comes into agreement, the
    # second keeps its own key rather than taking over the first one's.
    loop = {"stopList": {c: stop(c) for c in ("L1", "L2", "L3")},
            "routeList": {"8+1+A+B": route(["kmb"], "8", "O", ["L1", "L2", "L1", "L3"])}}

    def kmb(*fares):
        return lambda url, tries=3: loop if url == farewatch.DB_URL else {
            "data": {"routeStops": [{"CName": "站 (%s)" % c, "AirFare": f} for c, f
                                    in zip(("L1", "L2", "L1", "L3"), fares)]}}
    first, _ = run(kmb("6.0", "5.0", "6.0", "0"))
    state, _ = run(kmb("5.0", "5.0", "6.0", "0"), first)
    title = state["entries"][0].split("<title>")[1].split("</title>")[0]
    second = [k for k, d in first["divergences"].items() if d["seq"] == 2]
    if (len(first["divergences"]) != 2 or title != "Fare divergence: 1 resolved"
            or list(state["divergences"]) != second):
        failures.append("a stop served twice lost or swapped a finding: %r, then "
                        "%r, %r" % (sorted(first["divergences"]), title,
                                    sorted(state["divergences"])))

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("ok: %d targets compared, divergences on %r" % (coverage["targets"], routes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
