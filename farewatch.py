#!/usr/bin/env python3
"""Watch for HK bus fares that disagree with the operator's own published fare.

hkbus.app renders the fares in the Transport Department's GTFS feed. When that
feed disagrees with KMB, riders see the wrong price and nothing notices. This
compares the two once a day and publishes an Atom feed of the CHANGES, so the
maintainer can subscribe instead of being told.

Stdlib only. Writes state.json, feed.xml and report.md next to itself.
"""

import json
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
DB_URL = "https://data.hkbus.app/routeFareList.min.json"
KMB_URL = ("https://search.kmb.hk/KMBWebSite/Function/FunctionRequest.ashx"
           "?action=getstops&route={route}&bound={bound}&serviceType={st:02d}")
FEED_URL = "https://evnchn.github.io/hkbus-fare-watch/feed.xml"
REPORT_URL = "https://github.com/evnchn/hkbus-fare-watch/blob/main/report.md"
UA = {"User-Agent": "hkbus-fare-watch (+https://github.com/evnchn/hkbus-fare-watch)"}
BOUND = {"O": 1, "I": 2}
WORKERS = int(os.environ.get("WORKERS", "6"))


def get(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as f:
                return json.load(f)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def stop_code(name):
    """Trailing "(KT460)" -> KT460."""
    if name.endswith(")") and "(" in name:
        return name[name.rindex("(") + 1:-1]
    return None


def stop_id(code, name):
    """What a finding is keyed on: the stop, not its index on the route."""
    return code or stop_code(name) or name


def sweep():
    """Return {key: divergence} plus coverage counters."""
    db = get(DB_URL)
    route_list, stop_list = db["routeList"], db["stopList"]

    targets = []
    for key, route in route_list.items():
        if "kmb" not in (route.get("co") or []) or not route.get("fares"):
            continue
        bound = route.get("bound", {}).get("kmb")
        if bound in BOUND:
            targets.append((key, route, bound))

    def check(item):
        key, route, bound = item
        url = KMB_URL.format(route=route["route"], bound=BOUND[bound],
                             st=int(route.get("serviceType", 1)))
        try:
            rows = get(url)["data"]["routeStops"]
        except Exception as e:
            return ("failed", key, repr(e)[:100], set())

        stops = route["stops"]["kmb"]
        if len(rows) != len(stops):
            return ("skipped", key, "stop count", set())
        theirs = [stop_code(r["CName"]) for r in rows]
        ours = [stop_code(stop_list[s]["name"]["zh"]) for s in stops]
        voted = [(a, b) for a, b in zip(theirs, ours) if a and b]
        if not voted:  # nothing to check the stop order against
            return ("skipped", key, "no stop codes", set())
        if any(a != b for a, b in voted):
            return ("skipped", key, "stop codes", set())

        idents = [stop_id(c, stop_list[s]["name"]["zh"]) for c, s in zip(theirs, stops)]
        # a stop the route serves again is told apart by visit, not by index
        idents = ["%s|%s" % (key, x) + ("|%d" % n if n else "")
                  for x, n in ((x, idents[:i].count(x)) for i, x in enumerate(idents))]
        found, seen = {}, set()
        for i in range(min(len(route["fares"]), len(rows) - 1)):
            try:
                mine = float(route["fares"][i])
                kmb = float(rows[i]["AirFare"])
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(mine) and math.isfinite(kmb)):
                continue  # float("NaN") parses, and compares false against all
            if kmb == 0:  # KMB publishes no fare for that boarding stop
                continue
            seen.add(idents[i])
            if abs(mine - kmb) > 0.001:
                found[idents[i]] = {
                    "route": route["route"], "bound": bound,
                    "serviceType": route.get("serviceType"), "seq": i,
                    "stop": theirs[i], "stopName": stop_list[stops[i]]["name"]["zh"],
                    "app": mine, "kmb": kmb,
                }
        return ("ok", key, (found, idents), seen)

    divergences, skipped, failed, compared = {}, {}, [], 0
    seen, unobserved, present = set(), set(), {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for kind, key, detail, looked_at in pool.map(check, targets):
            seen |= looked_at
            if kind == "ok":
                compared += 1
                divergences.update(detail[0])
                present[key] = detail[1]
                continue
            unobserved.add(key)
            if kind == "skipped":
                skipped[detail] = skipped.get(detail, 0) + 1
            else:
                failed.append((key, detail))
    coverage = {"targets": len(targets), "compared": compared,
                "skipped": skipped, "failed": len(failed),
                "unobserved": sorted(unobserved)}
    return divergences, coverage, {"seen": seen, "routes": set(route_list),
                                   "present": present}


def carry_forward(old, new, evidence):
    """Keep findings this run did not actually look at.

    A finding leaves `new` for two very different reasons: the fare was compared
    and now agrees, or nothing ever looked at it — the request failed, a skip
    guard fired, the route lost its fares, KMB published 0 for that stop. Only
    the first is agreement. Without this, the second announces stops that "now
    agree" on the strength of a comparison that never happened, and drops them
    from state so they return as "new" tomorrow.

    A route that has left the feed entirely is gone rather than unobserved, and
    is allowed to resolve, and so is a stop that has left a route compared this run.
    """
    def gone(k):
        route = k.split("|", 1)[0]
        if route not in evidence["routes"]:
            return True
        if route not in evidence["present"]:  # not compared this run
            return False
        now = evidence["present"][route]
        if k.split("|", 1)[1].isdigit():  # a legacy key migrate() could not place
            stop = [route, stop_id(old[k]["stop"], old[k]["stopName"])]
            visits = [x for x in now if x.split("|")[:2] == stop]
            if visits and all(x in evidence["seen"] and x not in new for x in visits):
                evidence["seen"].add(k)  # every visit it could be was compared and agrees
                return True
            return not visits
        return k not in now
    carried = [k for k in old
               if k not in new and k not in evidence["seen"] and not gone(k)]
    for k in carried:
        new[k] = old[k]
    return carried


def migrate(state, evidence):
    """Re-key findings stored as "<route>|<index>" the way sweep() keys them now."""
    renamed = {}
    for k, d in sorted(state["divergences"].items(), key=lambda kv: kv[1]["seq"]):
        route_key, rest = k.split("|", 1)
        if rest == str(d["seq"]):
            stop = [route_key, stop_id(d["stop"], d["stopName"])]
            now = evidence["present"].get(route_key, [])
            same = [x for x in now if x.split("|")[:2] == stop]
            if d["seq"] < len(now) and now[d["seq"]] in same:
                renamed[k] = now[d["seq"]]    # this run saw the same stop there
            elif len(same) == 1:
                renamed[k] = same[0]          # it moved along the route
            # otherwise keep the old key until a run can tell which visit it was
    state["divergences"] = {renamed.get(k, k): d for k, d in state["divergences"].items()}
    state["unchecked"] = sorted(renamed.get(k, k) for k in state.get("unchecked", []))


def diff(old, new):
    appeared = [new[k] for k in new if k not in old]
    resolved = [old[k] for k in old if k not in new]
    changed = [dict(new[k], wasApp=old[k]["app"], wasKmb=old[k]["kmb"])
               for k in new if k in old and
               (new[k]["app"] != old[k]["app"] or new[k]["kmb"] != old[k]["kmb"])]
    return appeared, resolved, changed


def variant(d):
    st = str(d.get("serviceType") or 1)
    return "" if st == "1" else " (service type %s)" % st


def order(d):
    """Keep each service type's stops together; they share a route and bound."""
    return d["route"], d["bound"], str(d.get("serviceType") or 1), d["seq"]


def describe(d, past=False, stale=False):
    where = "%s %sbound%s at %s" % (
        d["route"], "out" if d["bound"] == "O" else "in", variant(d), d["stopName"])
    if stale:
        return "%s: last seen at $%.1f against KMB's $%.1f, not verified this run" % (
            where, d["app"], d["kmb"])
    if past:
        return "%s: was $%.1f against KMB's $%.1f, %s" % (
            where, d["app"], d["kmb"],
            "no longer on the route" if d.get("gone") else "now agrees")
    return "%s: app shows $%.1f, KMB publishes $%.1f" % (where, d["app"], d["kmb"])


def render_entry(stamp, appeared, resolved, changed, totals, unchecked=()):
    bits = []
    if appeared:
        bits.append("%d new" % len(appeared))
    if resolved:
        bits.append("%d resolved" % len(resolved))
    if changed:
        bits.append("%d changed" % len(changed))
    if unchecked:
        bits.append("%d not checked" % len(unchecked))
    title = "Fare divergence: " + ", ".join(bits)

    lines = []
    for label, group, past, stale in (
            ("Now diverging", appeared, False, False),
            ("No longer diverging", resolved, True, False),
            ("Amount changed", changed, False, False),
            ("Not checked this run", unchecked, False, True)):
        if not group:
            continue
        lines.append("<h3>%s</h3><ul>" % label)
        ordered = sorted(group, key=order)
        for d in ordered[:25]:
            lines.append("<li>%s</li>" % escape(describe(d, past, stale)))
        if len(ordered) > 25 and past:
            lines.append("<li>and %d more</li>" % (len(ordered) - 25))
        elif len(ordered) > 25:
            lines.append('<li>and %d more, in the <a href="%s">standing list</a>'
                         "</li>" % (len(ordered) - 25, REPORT_URL))
        lines.append("</ul>")
    note = (", %d of them not verified this run" % len(unchecked)) if unchecked else ""
    lines.append("<p>%d standing findings%s, across %d compared "
                 "route directions.</p>" % (totals["stops"], note, totals["compared"]))
    body = "".join(lines)

    return ("  <entry>\n"
            "    <title>%s</title>\n"
            "    <id>tag:evnchn.github.io,2026:hkbus-fare-watch/%s</id>\n"
            "    <updated>%s</updated>\n"
            "    <content type=\"html\">%s</content>\n"
            "  </entry>\n" % (escape(title), stamp, stamp, escape(body)))


def main():
    state_path = os.path.join(HERE, "state.json")
    feed_path = os.path.join(HERE, "feed.xml")

    try:
        state = json.load(open(state_path, encoding="utf-8"))
    except FileNotFoundError:
        state = {"divergences": {}, "entries": []}

    divergences, coverage, evidence = sweep()
    migrate(state, evidence)
    carried = carry_forward(state["divergences"], divergences, evidence)
    coverage["carried"] = len(carried)
    for k, d in state["divergences"].items():
        if k not in divergences and k not in evidence["seen"]:
            d["gone"] = True  # resolved by leaving, not by a compared agreement
    appeared, resolved, changed = diff(state["divergences"], divergences)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    totals = {"stops": len(divergences), "compared": coverage["compared"]}
    # Announce carried findings when the set CHANGES. A route that stays skipped
    # is still unverified, but repeating it daily would bury the real changes.
    newly_unchecked = sorted(carried) != state.get("unchecked", [])
    unchecked = [divergences[k] for k in carried] if newly_unchecked else []
    if appeared or resolved or changed or unchecked:
        state["entries"].insert(0, render_entry(stamp, appeared, resolved,
                                                changed, totals, unchecked))
        state["entries"] = state["entries"][:50]
        state["published"] = stamp
        print("changes: %d new, %d resolved, %d changed, %d not checked"
              % (len(appeared), len(resolved), len(changed), len(unchecked)))
    elif not state["entries"]:
        # first ever run: publish the standing backlog so the feed is not empty
        state["entries"].insert(0, render_entry(stamp, list(divergences.values()),
                                                [], [], totals))
        state["published"] = stamp
        print("first run: %d standing divergences" % len(divergences))
    else:
        print("no change")

    state["divergences"] = divergences
    state["unchecked"] = sorted(carried)
    state["coverage"] = coverage
    state["updated"] = stamp
    state.setdefault("published", stamp)
    json.dump(state, open(state_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom">\n'
                '  <title>hkbus fare divergence watch</title>\n'
                '  <link href="%s" rel="self"/>\n'
                '  <id>tag:evnchn.github.io,2026:hkbus-fare-watch</id>\n'
                '  <author><name>hkbus fare divergence watch</name>'
                '<uri>https://github.com/evnchn/hkbus-fare-watch</uri></author>\n'
                '  <updated>%s</updated>\n'
                % (FEED_URL, state.get("published", stamp)))
        f.write("".join(state["entries"]))
        f.write("</feed>\n")

    with open(os.path.join(HERE, "report.md"), "w", encoding="utf-8") as f:
        f.write("# Standing fare divergences\n\n")
        f.write("Updated %s. %d stops across %d compared route directions "
                "(%d skipped, %d failed).\n\n"
                % (stamp, len(divergences), coverage["compared"],
                   sum(coverage["skipped"].values()), coverage["failed"]))
        f.write("| route | dir | stop | app | KMB |\n|---|---|---|---:|---:|\n")
        for d in sorted(divergences.values(), key=order):
            f.write("| %s%s | %s | %s | %.1f | %.1f |\n"
                    % (d["route"], variant(d), d["bound"], d["stopName"],
                       d["app"], d["kmb"]))


if __name__ == "__main__":
    main()
