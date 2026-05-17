# TODOs

Captured by /plan-eng-review on 2026-05-17 (heatmap design review),
updated 2026-05-17 (hotline-replaces-heat-blob design review).

## 1. Add a JS test framework for client-side regressions

**What.** Wire up vitest + jsdom (or playwright for browser-level) to enable
automated tests for `src/wishmap/static/index.html` logic. Today all client
behavior (filter logic, popup wiring, rating optimistic updates, drawer
sort, heat toggle, hotline density compute) is tested by eyeballing.

**Why.** The single-file vanilla-JS approach is a deliberate constraint
that's worth keeping — but as the IIFE grows past 1600 lines, the
probability of silent regressions rises. The hotline migration introduced
a regression-class change (`layer.feature = feature` must be set manually
on hotline layers, otherwise popup-ratings and the drawer break silently)
that would have been caught by a single unit test. The next feature that
touches `applyFilters()` will have the same risk profile.

**Pros.** Lock in the [[applyfilters-registry-lifecycle]] invariant with a
test. Make it cheap to add new client features confidently. Specific first
targets: extract `buildRouteHeatValues`, `matchesFilter`, `popupHtml`,
`drawerStars` as exports; write 5 unit tests each. The hotline-`feature`
attachment becomes a tested invariant rather than a manual-test
checklist item.

**Cons.** Adds a `package.json` and a build/test dep. Breaks the "no JS
toolchain" purity the project has held since day one. Maintenance cost
for one developer.

**Context.** The project gained server-side pytest (`tests/`, 7 files) in
recent commits — the precedent for *some* automated testing is now set.
Logical next step would be vitest+jsdom for unit-testable pure-function
extracts from the IIFE.

**Depends on.** Nothing.

## 2. ~~Monitor `leaflet.heat` for staleness~~ — OBSOLETE (resolved 2026-05-17)

**Resolution.** The hotline-replaces-heat-blob design replaces
`leaflet.heat@0.2.0` with `leaflet.hotline@0.4.0`. `leaflet.heat` is no
longer a dependency. The new TODO #4 below tracks the same staleness
concern for the replacement plugin.

## 3. Optimize heat layer reuse (defer until needed) — REVISIT POST-HOTLINE

**What.** With the hotline migration, the "remove + recreate" pattern is
now per-route instead of per-blob (859 hotline layers torn down and
rebuilt on every filter change). This is more allocation than the prior
single-blob approach.

**Why.** On rapid filter changes, recreating 859 canvas layers may show
perceived lag even when each individual layer is cheap. Pre-hotline, this
TODO targeted the single blob layer. Post-hotline, the per-route layer
count is the relevant axis.

**Pros.** Reuse existing hotline layers between filter changes by toggling
visibility (`layer.setStyle({opacity: 0})` or pane-level display:none)
instead of removing.

**Cons.** Adds significant state tracking — which routes match which
filters, which layers exist. The current "rebuild on filter change"
approach mirrors the existing L.geoJSON OFF-mode pattern and is much
simpler to reason about.

**Context.** Defer until profiling on the real dataset shows perceived
lag. Most filter changes will reduce the visible set significantly, so
the rebuild cost is amortized.

**Depends on.** Nothing. Pure refactor.

## 4. Monitor `leaflet.hotline` for staleness

**What.** Periodically check whether `leaflet.hotline@0.4.0` is still
compatible with the version of Leaflet we ship. The plugin hasn't seen
a meaningful update since ~2018. If Leaflet ever cuts a 2.x with breaking
changes, hotline rendering will silently fail.

**Why.** Single point of failure on an unmaintained dependency, identical
risk class to the resolved TODO #2. The SRI hash protects against
tampering, not abandonment.

**Pros.** Catch a future break before users do.

**Cons.** Low-frequency work, easy to forget.

**Context.** Upgrading Leaflet requires a deliberate audit. If
leaflet.hotline breaks, the replacement plan is: write a tiny custom
canvas overlay that draws per-vertex-gradient polylines (~80 lines —
larger than the leaflet.heat fallback was, because hotline is doing more
per pixel). Document the recipe here when the time comes.

**Depends on.** Future Leaflet major-version upgrade.

## 5. Strava route name prefill via `og:title`

**What.** Add `GET /api/strava/route-meta?url=...` that fetches the public
Strava route page and parses `og:title` to prefill the name field in the
"Add route from Strava" modal.

**Why.** The add-route feature (design 20260613, branch
`claude/improve-password-popup-ui`) ships v1 with the name prefilled from the
uploaded GPX `<trk><name>` only. If Strava route exports carry a blank or
generic `<name>`, the user types the name on every add. An `og:title` fetch
from the public page restores the "auto-fill then edit" UX.

**Pros.** Nicer capture UX; no auth needed (public page); cheap endpoint.

**Cons.** Fragile to Strava markup changes; an extra endpoint to maintain;
may be entirely redundant if the GPX `<name>` is already good.

**Context.** Deferred from the add-route eng review (2026-06-14). The decision
hinges on a five-minute experiment: export the GPX of a route you do NOT own
and inspect its `<trk><name>`. If it's usable, this TODO is unnecessary — drop
it. If blank/generic, build it.

**Depends on.** The add-route feature landing first; the GPX-`<name>`
inspection finding.

## 6. Public-export client cold-start (thin/paginate the corpus)

**What.** For the public read-only export, reduce the in-browser cold-start
cost: today the page ships the full ~859-route corpus and runs the two-pass
density compute (`buildRouteHeatCoords` + `buildRouteHeatValues`,
index.html:1206/1230) on every load. Options when it matters: pre-thin/round
coordinates at export time, lazy-load routes, or a lighter public render path.

**Why.** This is exactly the live app's current behavior — *not* a regression
introduced by the export. But the public showcase is the first time non-Hendrik,
non-laptop visitors hit it; a phone on a slow link gets the same heavy boot.
"Everything as-is" was settled in office-hours as a *data* decision (what
strangers see), not a *client-performance* decision. Recording so the insight
isn't re-discovered via a "why is the map slow on mobile" complaint.

**Pros.** Faster public load, lower bandwidth, better mobile experience.

**Cons.** Adds export-time processing or a second render path; in tension with
the "reuse the one `index.html`, no fork" constraint. Premature before a real
slow-device measurement.

**Context.** Codex outside-voice finding #5 during the 2026-06-21 public-snapshot
eng review. Relates to existing TODOS #1 (JS test framework) and #3 (hotline
layer reuse) — both touch client perf. Measure first on a real phone before
building.

**Depends on.** The `wishmap export` feature landing first; a real
slow-device/slow-link measurement showing perceived lag.
