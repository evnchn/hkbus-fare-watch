# RESULTS — issues #3 and #5

**Verdict: both fixed on `cloud/stop-identity`. The full suite passes, and each new test fails with its fix reverted. Not deployed, the workflow was not run, and no feed was published.**

## What I did

Branch `cloud/stop-identity` from `main` @ `3d63087`. Four commits:

| commit | change |
|---|---|
| `e36ee74` | **#3.** A route where no stop carries a code on *both* sides is skipped as `"no stop codes"` instead of passing the guard having checked nothing. README now says exactly what is checked. |
| `ff5b4fa` | **#5.** A finding is keyed `<routeKey>\|<stop>` instead of `<routeKey>\|<index>`. `<stop>` is `stop_id()`: KMB's code, else the code in our stop name, else our stop name (for the 28 current findings with no code). |
| `6ea7f89` | **#5 migration.** `migrate()` re-keys stored `\|<index>` findings from each record's own `stop`/`stopName` before diffing, so the first run after deploy does not publish every finding as resolved and new. |
| `cac67c1` | **#5, a bug the skeptic found in `ff5b4fa`.** A stop a route serves twice gets `\|<index>` on its later visits. Which visit is "first" is decided from the route's whole stop list, not the order among diverging stops, so one visit resolving cannot hand its key to the other. |

Tests added to `test_farewatch.py`, in its existing failures-list style. `run()` drives `main()` in a temp dir, so the repo's `state.json`/`feed.xml`/`report.md` are never written.
- **#3:** a route with no codes on either side must be skipped as `no stop codes`.
- **#5:** J2 is replaced by J4 at the same index in both sources, with the same diverging fares. It must publish an entry, and `106+1+A+B|J4` must be a key.
- **#5 migration:** an old-format state with nothing changed must publish nothing.
- **#5 loop route:** L1 is served twice. When the first visit agrees, the second must keep its own key.

Issues read via the public issue pages: `gh` is not installed, and the GitHub API returned "access not enabled" for this session. PRs 4 and 6 fetched with `git fetch <repo> refs/pull/N/head`. `data.hkbus.app` is blocked by the egress proxy, so I could not measure effects on live data. The real-data check below uses the committed `state.json`, read-only.

## Commands and real output

Full suite at HEAD (`cac67c1`):
```
$ python3 test_farewatch.py
ok: 2 targets compared, divergences on ['106', '1A']
exit=0
```

Each new test failing with its fix reverted. Each command reverts `farewatch.py` only, keeps HEAD's tests, and resets afterwards.

#3:
```
$ git diff e36ee74^ e36ee74 -- farewatch.py | git apply -R --3way && git diff --stat HEAD && python3 test_farewatch.py; git reset -q --hard HEAD
Applied patch to 'farewatch.py' cleanly.
 farewatch.py | 5 +----
 1 file changed, 1 insertion(+), 4 deletions(-)
FAIL: a route with no stop codes on either side passed the stop-code guard unchecked: {'9+1+A+B|站B2': {'route': '9', 'bound': 'O', 'serviceType': 1, 'seq': 1, 'stop': None, 'stopName': '站B2', 'app': 5.0, 'kmb': 6.0}}, {'targets': 1, 'compared': 1, 'skipped': {}, 'failed': 0}
exit=1
```

#5 re-key (all three #5 commits reverted, which is `main`'s keying):
```
$ git revert -n cac67c1 6ea7f89 ff5b4fa && git checkout HEAD -- test_farewatch.py README.md && python3 test_farewatch.py; git reset -q --hard HEAD
 farewatch.py | 26 ++------------------------
 1 file changed, 2 insertions(+), 24 deletions(-)
FAIL: a stop replaced at the same index inherited the old stop's finding silently: published=False, keys=['106+1+A+B|1', '1A+1+A+B|1']
exit=1
```

#5 migration only:
```
$ git diff 6ea7f89^ 6ea7f89 -- farewatch.py | git apply -R && python3 test_farewatch.py; git checkout -- farewatch.py
FAIL: re-keying state.json published unchanged findings as resolved and new: '  <entry>\n    <title>Fare divergence: 2 new, 2 resolved</title>\n    <id>tag:evnchn.github.io,2026:hkbus-fare-watch/2026-09-24T01:47:39Z</id>\n    <updated>2026-09-24T01:47:39Z</updated>\n    <content type="html">&lt;h3&gt;Now diverging&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;106 inbound at 站 (J2): app shows $5.0, KMB publishes $6.0&lt;/li&gt;&lt;li&gt;1A outbound at 站 (S2): app shows $5.0, KMB publishes $6.0&lt;/li&gt;&lt;/ul&gt;&lt;h3&gt;Back in agreement&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;106 inbound at 站 (J2): was $5.0 against KMB\'s $6.0, now agrees&lt;/li&gt;&lt;li&gt;1A outbound at 站 (S2): was $5.0 against KMB\'s $6.0, now agrees&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;2 stops diverging in total, across 2 compared route directions.&lt;/p&gt;</content>\n  </entry>\n'
exit=1
```

#5 loop-route key only:
```
$ git diff cac67c1^ cac67c1 -- farewatch.py | git apply -R && python3 test_farewatch.py; git checkout -- farewatch.py
FAIL: a stop served twice lost or swapped a finding: ['8+1+A+B|L1', '8+1+A+B|L1|2'], then 'Fare divergence: 1 resolved', ['8+1+A+B|L1']
exit=1

```

Migration dry run on the committed `state.json` (read-only, from the shell):
```
$ python3 -c '
import json, farewatch
old = json.load(open("state.json"))["divergences"]
new = farewatch.migrate(old)
print(len(old), "->", len(new), "keys; suffixed:", [k for k in new if k.count("|")>1])
print("idempotent:", farewatch.migrate(new) == new)
print("sample:", [k for k in new if new[k]["stop"] is None][:2])
'
172 -> 172 keys; suffixed: []
idempotent: True
sample: ['118+3+CROSS HARBOUR TUNNEL+SIU SAI WAN (ISLAND RESORT)|阿公岩道', '118+3+CROSS HARBOUR TUNNEL+SIU SAI WAN (ISLAND RESORT)|鯉魚門公園']
```

## Overlap with open PRs 4 and 6 (trial merges, not predictions)

```
$ git merge-tree --write-tree --name-only cloud/stop-identity pr4   # pr4 = refs/pull/4/head @ 8ff1ca6
520dc96d86ec5114f2ba31460a3ab7f371f35536
farewatch.py
test_farewatch.py

Auto-merging farewatch.py
CONFLICT (content): Merge conflict in farewatch.py
Auto-merging test_farewatch.py
CONFLICT (content): Merge conflict in test_farewatch.py
exit=1

$ git merge-tree --write-tree --name-only cloud/stop-identity pr6   # pr6 = refs/pull/6/head @ 1dedc76
45796154388fc42c8f2ed2088442d2ffe630f78b
exit=0

$ (scratch worktree: cloud/stop-identity merged with pr6) python3 test_farewatch.py && python3 test_feed.py
ok: 2 targets compared, divergences on ['106', '1A']
ok:   the_feed_declares_an_author
ok:   a_quiet_day_leaves_the_feed_untouched
ok:   a_state_file_predating_this_key_settles_down
ok:   a_resolved_overflow_does_not_link_to_the_standing_list
ok:   a_truncated_list_points_at_the_standing_list
exit=0

$ git diff --stat main..cloud/stop-identity -- state.json feed.xml report.md .github
(empty = untouched)
```

**PR 6** (`fix/feed-conformance`): merges cleanly, and both test files pass on the merged tree. No overlap beyond both touching `main()`.

**PR 4** (`fix/carry-unobserved-findings`) conflicts textually and semantically. Whichever lands second must resolve:
1. **`check()` return shape.** PR 4 makes skips 4-tuples (`("skipped", key, reason, set())`). My new `("skipped", "no stop codes", None)` must follow suit. Conflict hunk at the stop-code guard.
2. **`main()`.** PR 4 calls `carry_forward(state["divergences"], …)`; I call `diff(migrate(state["divergences"]), …)`. Migrate first, then carry forward. Conflict hunk.
3. **Silent, auto-merges:** PR 4's `seen.add("%s|%d" % (key, i))` is positional. It must add the new identity key (the same `ident` as `found`). Otherwise no finding is ever "seen", and every resolution is carried forward and never published.
4. **`carry_forward`'s `k.rsplit("|", 1)[0]`** gives the wrong route key for a suffixed loop-stop key (`route|L1|2`). Use `k.split("|", 1)[0]`.
5. **Identity keys interact with carry-forward.** A replaced stop's old key is never "seen" again, so PR 4 would carry it forever unless the stop's absence from the route counts as gone, not unobserved. PR 4's docstring already names this issue as "a separate defect".
6. **`test_farewatch.py`.** PR 4 rewrites it wholesale into `@case` functions. My four tests would need porting into that structure (its `run()`/`stub()` helpers are close to mine).

## Known limits, not fixed here
- **Issue #5's own example still says "J2 … now agrees"** when J4 *agrees*: J2 left the route and was never compared. It now comes from J2's own key, not a positional one, but "a stop that left the route" still reads as "agreement". That is #4's unobserved-vs-agreed territory.
- **Churn from name-keyed stops.** Codeless stops are keyed by our zh name, so a renamed stop reads as one resolved plus one new. The same applies to a stop whose code appears on one side only on some days. None of the current data has one-sided codes.
- **Newly skipped routes.** If a route newly hits `no stop codes` (KMB drops its codes for a day), its standing findings publish as "now agree". That is the same pre-existing behaviour as any other skip, which PR 4 addresses. All routes in the current `state.json` have at least one both-sides code, so this does not fire on deploy.
- **Migration of doubled stops.** `migrate()` suffixes a doubled stop by order among *stored findings*, while `sweep()` uses the full stop list. They can disagree once, for a doubled stop whose first visit was not diverging. There are no such keys in the current state (0 suffixed).
- **Scope of the #3 guard.** It requires ≥1 voting stop, not a proportion. The issue offered "a minimum number" or a README fix; I did the minimal code fix plus an exact README.

## Push
**Not pushed. The push was refused by the session's git proxy**, and I did not route around it. The branch exists only in this session's local clone. To publish it, add `evnchn/hkbus-fare-watch` to the session's sources and rerun the push.

```
$ git remote add origin https://github.com/evnchn/hkbus-fare-watch.git
$ git push -u origin cloud/stop-identity
remote: access denied by the git proxy: evnchn/hkbus-fare-watch is not in this session's authorized repository set, so the proxy will not inject a credential for it. To fix, add the repository to the session's sources.
fatal: unable to access 'https://github.com/evnchn/hkbus-fare-watch.git/': The requested URL returned error: 403
```
