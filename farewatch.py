#!/usr/bin/env python3
"""Watch for HK bus fares that disagree with the operator's own published fare.

hkbus.app renders the fares in the Transport Department's GTFS feed. When that
feed disagrees with KMB, riders see the wrong price and nothing notices. This
compares the two once a day and publishes an Atom feed of the CHANGES, so the
maintainer can subscribe instead of being told.

Stdlib only. Writes state.json, feed.xml and report.md next to itself.
"""

import json
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


def sweep():
    """Return {key: divergence} plus coverage counters."""
    db = get(DB_URL)
    route_list, stop_list = db["routeList"], db["stopList"]

    targets = []
    for key, route in route_list.items():
        if route.get("co") != ["kmb"] or not route.get("fares"):
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
            return ("failed", key, repr(e)[:100])

        stops = route["stops"]["kmb"]
        if len(rows) != len(stops):
            return ("skipped", "stop count", None)
        theirs = [stop_code(r["CName"]) for r in rows]
        ours = [stop_code(stop_list[s]["name"]["zh"]) for s in stops]
        if any(a and b and a != b for a, b in zip(theirs, ours)):
            return ("skipped", "stop codes", None)

        found = {}
        for i in range(min(len(route["fares"]), len(rows) - 1)):
            try:
                mine = float(route["fares"][i])
                kmb = float(rows[i]["AirFare"])
            except (TypeError, ValueError):
                continue
            if kmb == 0:  # KMB publishes no fare for that boarding stop
                continue
            if abs(mine - kmb) > 0.001:
                found["%s|%d" % (key, i)] = {
                    "route": route["route"], "bound": bound,
                    "serviceType": route.get("serviceType"), "seq": i,
                    "stop": theirs[i], "stopName": stop_list[stops[i]]["name"]["zh"],
                    "app": mine, "kmb": kmb,
                }
        return ("ok", found, len(rows))

    divergences, skipped, failed, compared = {}, {}, [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for kind, a, b in pool.map(check, targets):
            if kind == "ok":
                compared += 1
                divergences.update(a)
            elif kind == "skipped":
                skipped[a] = skipped.get(a, 0) + 1
            else:
                failed.append((a, b))
    return divergences, {"targets": len(targets), "compared": compared,
                         "skipped": skipped, "failed": len(failed)}


def diff(old, new):
    appeared = [new[k] for k in new if k not in old]
    resolved = [old[k] for k in old if k not in new]
    changed = [dict(new[k], wasApp=old[k]["app"], wasKmb=old[k]["kmb"])
               for k in new if k in old and
               (new[k]["app"] != old[k]["app"] or new[k]["kmb"] != old[k]["kmb"])]
    return appeared, resolved, changed


def describe(d, past=False):
    st = "" if str(d.get("serviceType", 1)) == "1" else " (service type %s)" % d["serviceType"]
    where = "%s %sbound%s at %s" % (
        d["route"], "out" if d["bound"] == "O" else "in", st, d["stopName"])
    if past:
        return "%s: was $%.1f against KMB's $%.1f, now agrees" % (
            where, d["app"], d["kmb"])
    return "%s: app shows $%.1f, KMB publishes $%.1f" % (where, d["app"], d["kmb"])


def render_entry(stamp, appeared, resolved, changed, totals):
    bits = []
    if appeared:
        bits.append("%d new" % len(appeared))
    if resolved:
        bits.append("%d resolved" % len(resolved))
    if changed:
        bits.append("%d changed" % len(changed))
    title = "Fare divergence: " + ", ".join(bits)

    lines = []
    for label, group, past in (("Now diverging", appeared, False),
                               ("Back in agreement", resolved, True),
                               ("Amount changed", changed, False)):
        if not group:
            continue
        lines.append("<h3>%s</h3><ul>" % label)
        ordered = sorted(group, key=lambda d: (d["route"], d["bound"], d["seq"]))
        for d in ordered[:25]:
            lines.append("<li>%s</li>" % escape(describe(d, past)))
        if len(ordered) > 25:
            lines.append("<li>and %d more</li>" % (len(ordered) - 25))
        lines.append("</ul>")
    lines.append("<p>%d stops diverging in total, across %d compared "
                 "route directions.</p>" % (totals["stops"], totals["compared"]))
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

    divergences, coverage = sweep()
    appeared, resolved, changed = diff(state["divergences"], divergences)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    totals = {"stops": len(divergences), "compared": coverage["compared"]}
    if appeared or resolved or changed:
        state["entries"].insert(0, render_entry(stamp, appeared, resolved,
                                                changed, totals))
        state["entries"] = state["entries"][:50]
        print("changes: %d new, %d resolved, %d changed"
              % (len(appeared), len(resolved), len(changed)))
    elif not state["entries"]:
        # first ever run: publish the standing backlog so the feed is not empty
        state["entries"].insert(0, render_entry(stamp, list(divergences.values()),
                                                [], [], totals))
        print("first run: %d standing divergences" % len(divergences))
    else:
        print("no change")

    state["divergences"] = divergences
    state["coverage"] = coverage
    state["updated"] = stamp
    json.dump(state, open(state_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom">\n'
                '  <title>hkbus fare divergence watch</title>\n'
                '  <link href="%s" rel="self"/>\n'
                '  <id>tag:evnchn.github.io,2026:hkbus-fare-watch</id>\n'
                '  <updated>%s</updated>\n' % (FEED_URL, stamp))
        f.write("".join(state["entries"]))
        f.write("</feed>\n")

    with open(os.path.join(HERE, "report.md"), "w", encoding="utf-8") as f:
        f.write("# Standing fare divergences\n\n")
        f.write("Updated %s. %d stops across %d compared route directions "
                "(%d skipped, %d failed).\n\n"
                % (stamp, len(divergences), coverage["compared"],
                   sum(coverage["skipped"].values()), coverage["failed"]))
        f.write("| route | dir | stop | app | KMB |\n|---|---|---|---:|---:|\n")
        for d in sorted(divergences.values(),
                        key=lambda d: (d["route"], d["bound"], d["seq"])):
            f.write("| %s | %s | %s | %.1f | %.1f |\n"
                    % (d["route"], d["bound"], d["stopName"],
                       d["app"], d["kmb"]))


if __name__ == "__main__":
    main()
