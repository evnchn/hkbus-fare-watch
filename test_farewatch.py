#!/usr/bin/env python3
"""Offline tests for the sweep. Stdlib only: python3 test_farewatch.py"""

import sys

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

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("ok: %d targets compared, divergences on %r" % (coverage["targets"], routes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
