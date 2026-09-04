# hkbus fare divergence watch

[hkbus.app](https://hkbus.app) shows the per-stop fares published in the Transport
Department's GTFS feed. When that feed disagrees with the operator's own published
fare, riders see the wrong price and nothing notices.

This compares the two once a day and publishes the **changes** as an Atom feed.

- **Feed:** [`feed.xml`](feed.xml) — subscribe in any reader
- **Standing list:** [`report.md`](report.md) — everything currently diverging

An entry is only published when something moves: a stop starts diverging, stops
diverging, or the amount changes. A quiet day publishes nothing.

## What it does and does not claim

The app renders the TD feed faithfully. Where this feed reports a divergence, it is
the **data sources disagreeing** — usually the GTFS carrying a stale sectional fare
after a service change. Which one is right is not decided here; KMB's own published
figure is simply the more likely candidate.

Comparison only runs where a route's stop count and stop codes match on both sides,
so a stop-ordering defect is skipped rather than misreported as a fare defect. Stops
KMB publishes no fare for (`AirFare` 0) are skipped. `report.md` states what was
skipped rather than implying full coverage.

Fares come from `search.kmb.hk`, the KMB website's own endpoint, at roughly 1,300
requests once a day. That is why this lives here rather than in the crawler: it is
not part of the government open-data set the crawler is built on.
