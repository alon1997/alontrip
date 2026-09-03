# Trip Algorithm (Prose Version)

This page follows the code. Change the algorithm in `backend/app/services/planner.py` first (the rule-based fallback lives in `grouping.py`), then change this page. For the HTTP surface see [API.md](API.md). Product scope and the task queue live in the private planning docs and are deliberately not repeated here — this page is not a work order.

Top half: what `plan_trip` actually does today. Bottom half: the rules that are already implemented — district grouping, per-day capacity, and opening-hours clipping of clock times.

`main.py` does no scheduling: it only queries transit between adjacent pairs after `plan_trip()` returns.

---

## What happens after Generate: one request, end to end

From clicking Generate on the planner page to the result page rendering, six steps run. **DeepSeek is called only in step 3 (once per city), SerpApi only in step 4 (one query per leg)** — ticking spots, toggling map layers, or changing hotels never touches either API.

| Step | Who | What happens |
|----|-----|--------|
| 1 | Frontend `Planner.vue` → `POST /api/trip/optimize-route` | Sends the city list, day count, spot ids, hotel mode, first/last-day density, arrival/departure hubs, and flight times |
| 2 | `main.py` validation | Cities legal, days ≥ city count and within 2–14, at least 1 spot per city, hubs in the right city, no orphan times, no malformed formats → every failure returns a frozen 400 message |
| 3 | `planner.py plan_trip()` day assignment | (a) split days across cities proportionally to spot counts; (b) build per-city context (opening windows, daily budget, clock start); (c) **DeepSeek draft** (one `deepseek-v4-flash` call per city, temperature 0.2, thinking disabled, 45 s timeout, one automatic retry on a failed draft; two cities run in parallel) — it must return JSON of "which spots each day + which hotel each night"; (d) validate the table and apply three repair layers (**id repair** → **spot-set repair** → **district repair**); a city that still fails falls back to **greedy NN-chain grouping** (see step 3 note), and one city's fallback never affects the others; (e) shared post-processing: parks ≥ 6 h take a whole day → capacity packing → opening-window relocation → capacity packing → **closing-time terminal check** (below); (f) within-day ordering: the NN-chain order itself is the route order (no 2-opt needed — the chain is already geographically contiguous) + earliest-closing-first + pull spots that would hit closing forward |
| 4 | `main.py` per-leg transit | For each adjacent pair on each day's chain: Japan/Korea/Hong Kong use **SerpApi** `google_maps_directions` (after checking `data/transit_cache/` — a hit costs nothing); mainland China uses AMap or a taxi estimate; straight-line under 800 m counts as walking. Every result lands in the leg cache |
| 5 | `schedule.py` clock schedule | Accumulates clock times from **real** transit durations (arrival-day start = landing + 90, departure cutoff = takeoff − 120, global 22:00 cap); lunch around 12:00 (1 h), dinner 18:00 (1.5 h); visits are clipped by opening hours — arriving early waits for opening, arriving after closing writes no visit row and raises an unvisited warning |
| 6 | Response → frontend | `itinerary` (per-day nodes/legs/clock schedule/costs) + `totals` (transit/tickets/lodging/grand total; unvisited spots are not billed) + `warnings` (unmovable and unvisited spots, reported honestly) + `grouper` (`deepseek` / `rule-based` / `mixed`, reported honestly) → `Result.vue` sidebar + `ResultMap.vue` map |

**Why the closing-time terminal check exists (T-045):** when assignment finishes at the grouping layer, only approximate clock times exist (25 minutes per hop). Without replaying real times you can end up with "a museum that closes at 17:00 scheduled as the 21:00 finale" — and the user's first reaction is "this looks amateur". The terminal check replays each day from its true start clock (arrival day = landing + 90 + 20 min check-in + airport rail at 1.2 min/km; intercity arrival day 16:00; every other day 09:00) and applies three rescue layers: (1) in-day reorder (move earlier-closing spots forward); (2) cross-day move (into a day that fits and still passes after insertion); (3) cross-day swap (trade for a spot on another day that can be visited in the evening). Only when all three fail does `warnings` report it honestly. `parse_hours` covers all 328 catalog entries (2026-08-28 audit: 0 failures); a spot whose closing time it cannot parse is treated as always open — so a broken parser blinds the whole chain to opening hours, and any change to it requires rerunning the full-catalog audit.

---

## What the code does today

Click Generate → `POST /optimize-route` → `plan_trip(...)`. Transit queries are not in this file.

### What it receives

| Input | Meaning |
|------|------|
| City list (1–4, ordered) | Finish Tokyo before Kyoto; cities never interleave |
| Days, 2–14 | Must be ≥ the number of cities |
| Selected spot ids | At least one per city, otherwise the request is rejected |
| Lodging mode | One hotel per city / may switch hotels / user picks each night |
| Optional: arrival hub, departure hub | Must belong to the first city and the last city respectively |
| Optional: landing time, takeoff time (T-034) | `HH:MM`, 24-hour, interpreted in the hub city's local time; must be submitted together with the matching hub — an orphan time is an immediate 400 |
| First/last-day density | The arrival/departure day should carry **as few spots as possible** (see step 3). `first_day_density` / `last_day_density`, each `none` or `few`; all four combinations are valid |

Validation failures return fixed English sentences (the frontend matches them verbatim; the wording must not drift).

When assigning, parks built for a full day (`suggested_duration_min` ≥ 360 — Disney, USJ, and the like) take a whole day to themselves and never mix with other spots. Everything else is clustered into geographic districts first, then a day's capacity is measured from stays + lunch + dinner + a rough transit estimate. After transit is queried, `schedule.py` accumulates real clock times: lunch around 12:00 (1 hour, window 11:30–14:00, cut as close to noon as possible) and splitting a visit never leaves a stub of ≤ 15 minutes. Dinner is 1.5 hours targeting **18:00** (T-038): if the last spot ends before 18:00, dinner waits until 18:00 (in practice every normal day starts at exactly 18:00); if it ends later, dinner starts immediately — the old 19:30 upper window is gone, so a late finish means a late dinner, still under the 22:00 cap. **No dinner is scheduled when the day's last stop is the departure hub.** Visits are clipped to `opening_hours` (arrive early → wait for opening; arrive after closing → no visit row).

### How flight times enter the algorithm (T-034)

Three user-confirmed defaults (2026-08-28): **landing buffer 90 minutes** (immigration + baggage + the ride into town), **takeoff buffer 120 minutes** (check-in + security), **day-end cap 22:00**. With no times submitted, behavior is exactly what it was before (09:00 start, no cutoff).

With times submitted, five things change:

1. **Day-1 clock start** = landing + 90, not 09:00. The first leg (hub → hotel) is queried in that hour's schedule bucket instead of the 09:00 bucket.
2. **Last-day cutoff** = takeoff − 120. Sightseeing must end before the cutoff, and dinner may not cross it.
3. **Daily capacity budgets shrink.** The comfortable target is still about 9 hours, but the arrival day's budget = 22:00 − start, and the departure day's = cutoff − 09:00 (floor 30 minutes). Concrete numbers go into the DeepSeek prompt ("flight lands at 14:30; sightseeing cannot start before 16:00").
4. **Window feasibility** (`_move_infeasible_for_windows`): three kinds of days have a sightseeing window — arrival day (start → 22:00), intercity arrival day (16:00 → 22:00; the city is only entered in the afternoon), and departure day (09:00 → cutoff). The window start is further reduced by a 60-minute real-world buffer (check-in + transit). A spot whose opening hours overlap its window by less than 30 minutes is **moved to the least-loaded feasible day**; if it cannot move (for example the only empty day is a `none` day that must stay empty), it stays where it is and `warnings` records `"<poi_id> cannot fit within opening hours on its assigned day"` — honest reporting, never silently swallowed. Feasibility must run **after** the `none` days are emptied: evacuate first, then check windows, otherwise evacuation pushes feasible spots into window days unnoticed (the Kinkaku-ji case).
5. **Global 22:00 cap:** no visit and no dinner is scheduled past 22:00 — no midnight itineraries, no 00:07 dinner. The cap trims visits and dinner only, never transit (transit time is real).

The response adds `arrival_start_min` / `departure_cutoff_min` (minute-of-day values, nullable); the frontend `ResultMap` clock mirror uses them instead of recomputing the buffers.

### Hard caps

At most 4 cities; 2–14 days. Selecting more than **3×(days−2)+2** spots (T-048 comfortable pace — 6 days → 14 spots) only warns, never rejects. The formula is derived backwards from capacity math: middle days lose roughly 150 minutes to two meals out of ~540 usable, at a comfortable 3 spots per day; the arrival and departure days are transit days worth about 2 spots between them. The planner page shows "selected / budget · comfortable / above pace" live in the spot picker (same formula front and back), so overload is visible before generating instead of arriving as a warning afterwards. Theme parks (≥ 6 h) take a whole day and are exempt from the pace count.

### Step 1: split the total days across cities

Code: `_split_city_days`.

1. Every city gets 1 day first.
2. The remaining days are split in proportion to each city's **selected spot count** (largest-remainder method). A city with 0 spots never reaches this step (rejected earlier).
3. City blocks are contiguous: Tokyo's 3 days are days 1–3, Kyoto's 2 days are days 4–5.

Example: Tokyo 8 spots, Kyoto 4 spots, 5 days → Tokyo 3 + Kyoto 2.

### Step 2: inside each city, spread its spots across its days

**With a DeepSeek key:** one model call for that city (`deepseek-v4-flash`, thinking disabled, 45-second timeout). The input is only that city's spots, its day count, the lodging mode, the first/last `none`/`few` flags, and **each spot's stay minutes and opening hours**. Rules baked into the prompt: group by district, do not split counts evenly, a park ≥ 360 minutes takes a whole day, a day is about 9 hours (stays + lunch 60 + dinner 90 + 25 per hop), only visit inside opening hours, and do not schedule dinner after the departure hub. The required output is JSON: which spots each day, which hotel each night. With thinking on by default, `max_tokens` was eaten and the call returned HTTP 200 with no JSON body (the Shanghai incident); requests now carry `thinking: disabled`.

The model output must satisfy all of the following, otherwise **the whole city is voided** (other cities unaffected) and rules take over. Failure logs at **error** level — it is not a "graceful degradation":

- The days must be exactly the city's allotment, 1…n with no gaps or duplicates.
- Every selected spot is used exactly once — **small slips are repaired first** (`_repair_poi_set`): a missing spot is inserted into the nearest non-empty day (never into a `none` day); a spot duplicated across days keeps the copy whose day centroid is closest. Up to 2 total missing/duplicate spots are repairable; more voids the city (production lesson from 2026-08-28: a 10+-spot table missing one spot used to void the whole city — far too expensive).
- **Mistyped ids are repaired first** (`_repair_poi_ids`, T-044): long model outputs scramble ids. After normalizing to `[a-z0-9]`, an id that matches **exactly one** catalog entry (equal, substring, or ordered word containment — e.g. dropping the `architectural` chunk) is rewritten; same-day duplicate ids are deduplicated; ambiguity, unknown ids, or scrambled word order still fall back. Request temperature is 0.2 (at high temperature the same prompt failed three different ways, per the 2026-08-28 production logs).
- Every day has at least 1 spot; only the arrival/departure `none` day the prompt allows may be an empty array.
- Hotel ids must match candidates in that city.
- One-hotel mode: the same hotel every day.
- Custom lodging: the model may not change it.

After a DeepSeek table passes validation, first/last days are **not** re-split by count (that would shatter the model's districts). Python still applies two cuts: a park with a stay ≥ 360 minutes takes a whole day; if the model put spots on a `none` arrival/departure day, they are moved off it, and then spots that push a day past ~9 hours (stays + meals + transit) are squeezed into other non-empty days. The model may forget a rule; code backstops it.

**Without a key, or if the model fails:** `_group_city_pois` → `RuleBasedGrouper` in `grouping.py`:

1. Cluster by coordinates (farthest points become the first centers, then iterate — deterministic, not random).
2. Merge clusters whose centroids are under ~1.2 km apart, then split/merge to the city's day count. Per-day counts are **no longer** forced to within ±1 of each other (that shattered districts).
3. Inside each day: nearest-neighbor chain starting from the geographic center of that day's spots.
4. Days are ordered "the more northern day first".
5. `planner.py` then estimates transit as stays + lunch 60 + dinner 90 + ~25 minutes per hop and moves spots that exceed the ~9-hour budget to the next day.

`planner.py` then applies its two cuts: first/last `none`/`few` counts, and whole-day parks ≥ 360 minutes (spots move in list order when cutting counts). A successful DeepSeek table must also pass the district check: if a spot sits far from its day's centroid and clearly closer to another day, **district repair runs first** — move the clearly misplaced spot to the nearest feasible day (at most 2 rounds, 4 spots, inserted at the position with the smallest distance increase); only if the table still fails does the whole city fall back to rules (before repair shipped, a single Ueno Park bundled into a west-suburb day voided the whole table — 3 consecutive `district split` fallbacks in production on 2026-08-28).

If a city's day count is **greater than** its spot count: one spot per day, spread evenly across the city block, with the leftover days empty (hotel/transit only). The same spot never appears on two days.

### Step 3: keep the first and last days light (transit days)

Product principle: **the first and last days are not ordinary sightseeing days.** Landing, check-in, and catching trains eat most of them.

- `none` = **0 spots** (transit / check-in / airport only). If every day of a city is `none` (a two-day trip with `none` on both ends, say) the spots cannot be dropped, so it falls back to even splitting rather than forcing both days empty.
- `few` = **as few as possible, not a hard 1.** When middle days can absorb, keep exactly 1; when middle days are already at ~5 spots/day (the same magnitude as the planner page's "days × 5" soft cap), the surplus stays on the `few` day. With no middle days available (typical: a two-day trip, 4–5 spots, last day `none`), the `few` day carries all the remaining spots — "few = 1" must never drop a spot or cram one into a `none` day.

Code: `_rebalance_edge_days` / `_edge_target_counts`: spread the spots across the middle days first, then cut the first/last days by the rules above. The even-split step itself does not favor edge days; "as few as possible" only happens here.

**API:** First day / Last day, each `none` or `few`. `planner` resolves both modes with `resolve_edge_modes`, then computes per-day counts. All four combinations are valid. Defaults remain First = few, Last = none.

A city with only 1 day and no neighbor to absorb spots is not force-cleared to 0 — that would empty the city's only day.

### Step 4: pick hotels (rule path only; a successful DeepSeek table supplies its own)

Candidates are that city's catalog hostels (`listed=1`). Straight-line distance only; no transit queries.

- **One hotel:** minimize the summed distance to the geographic centers of the "days that still have spots", and stay there for the whole block. A day with 0 spots does not score.
- **May switch hotels:** nearest to that day's last spot; if that is under 2 km from the previous night's hotel, stay there. A day with 0 spots reuses the previous day's hotel; if the first day is empty, pick from the center of all selected spots in the city.
- **Custom:** must cover every day 1…N; a missing day is rejected. The model may not change it.

### Step 5: assemble each day as a chain (input to the transit queries)

Code assembles `PlannedDay`; splicing the hubs into the chain happens in `main.py`'s `_build_day_chain`.

- Day 1's morning hotel = day 1's night hotel (a simplification: it ignores the afternoon check-in).
- Day N's morning hotel = day N−1's night hotel (on a city-change day you are still at the previous city's hotel in the morning and already in the next city by night).
- With an arrival hub and spots on day 1, the chain is `hub → hotel (check-in ~20 min) → spots… → that night's hotel`. Day 1 with 0 spots: hub → hotel. No hub selected: hotel → spots → hotel. Middle days and the last day get no check-in segment.
- With a departure hub, the last day's chain ends at the station/airport, and that day's "night hotel" is reported as the morning one (the chain's tail hotel is not really checked into).

The chain (day 1 with a hub and spots):

```
arrival hub → night hotel (check-in) → spot 1 → … → (departure hub or night hotel)
```

A day with 0 spots connects start straight to end (airport → hotel, or hotel → station).

Without hubs it is still hotel → spots → hotel.

### Transit queries (not in `planner.py`)

`main.py` queries transit for every adjacent pair on the chain. Intra-city at local 09:00, city-change at local 16:00. Straight-line under 800 m counts as walking.

- **Japan / Korea / Hong Kong:** SerpApi Google transit. A cache hit is not re-queried. Old cached answers of "No timetable + fare 0" are queried once more, and if there is still nothing they become a taxi estimate.
- **Mainland China:** AMap when `AMAP_KEY` is set; without a key, or if AMap comes back empty, a taxi estimate (**never Google** — empty results only burn quota).
- All amounts leave the door in **USD** (JPY/CNY converted at fixed rates). When no transit is found the leg reads `No public transit · taxi · $x · xx min`; a subway fare is never invented.

The 09:00 used for querying only asks "which services exist if I leave at this hour". The result page's clock times are computed separately by `schedule.py` (the frontend `ResultMap` keeps a mirror): accumulate transit + stays from 09:00, lunch around 12:00, dinner at 18:00 (1.5 h).

`ticket unknown` on a visit row means the spot's admission price is missing — it is not about transit. Priced tickets are converted to USD as well.

### Where the code differs from plan 6.1b (the code wins)

| The plan once said | The code actually does |
|----------|----------|
| Within a day, nearest-neighbor from the morning hotel | The rule path starts nearest-neighbor from the day's spot centroid; DeepSeek orders its own |
| At least 1 spot per day | First/last days may be 0 by density |
| Opening hours and visit duration decide "does it fit in the day" | Parks with a stay ≥ 360 minutes take a whole day; everything else is packed by stay + lunch + dinner + rough transit; clock times are clipped to `opening_hours`; no dinner after the departure hub |

---

## Implemented rules

### Dinner pinned to 18:00 (T-038, shipped)

`DINNER_EARLIEST = 18:00`. If the day's last spot ends before 18:00, dinner waits until 18:00 near that spot, then the chain returns to the hotel; if it ends after 18:00, dinner starts immediately (the old 17:20–19:30 window is deleted — a late finish means a late dinner). The cap is unchanged: dinner that would cross 22:00 (or the departure-day cutoff) is skipped entirely. The `ResultMap` mirror is in sync. Measured: normal days all start dinner at exactly 18:00; a day that finished at 19:33 started dinner at 19:33.

### Within-day order: 2-opt + earliest-closing-first (T-039 / T-036, shipped)

Every day passes through `_order_day` before the chain is emitted: first **2-opt** (deterministic; endpoint hotels excluded; skipped under 4 spots) to compress geographic detours; then **spots closing at or before 15:00 move to the front** in ascending closing order (Tsukiji market closing at 14:00 becomes the day's first stop, while the other spots keep their relative order). The same rules are written into the DeepSeek prompt. Measured: Tsukiji 09:25–11:25 first in its day, with zero opening-hours violations across all visits.

### Two-city DeepSeek in parallel (T-037, shipped)

`plan_trip` builds per-city context first (day split, budget, windows), then runs `_deepseek_fill_city` concurrently through a `ThreadPoolExecutor` (pure HTTP, no shared state). Measured: both cities' fill-completion timestamps matched to the millisecond; single-city behavior unchanged.

### Budget totals (T-040, shipped)

The response's `totals` adds `ticket_cost` (visits that actually happen; a spot split across lunch is billed once), `lodging_cost` (nights actually spent; the last night is not counted on the departure-hub day), `grand_total`, and `unknown_prices` (how many prices are missing — billed as $0 but counted honestly). Each day's `itinerary[].ticket_cost` is returned alongside. Tickets are only counted for visits that made it onto the clock schedule — a spot trimmed by opening hours is not billed (the Kinkaku-ji case). The result page shows `Trip budget ≈ $X` under Total transit with a transit/tickets/stays breakdown.

### Edge-day anchors and unvisited warnings (2026-08-28 bug hunt)

- **Anchor rules go into the prompt:** arrival-day spots pick "closest to that night's hotel" (drop the bags first); departure-day spots pick "closest to the departure hub" (on the way out). The model picks hotels itself, so neither rule needs extra data.
- **Unvisited warnings** (`main.py`): a spot listed in `poi_ids` that never got a visit row (the chain reached it after closing and `schedule.py` refuses to fabricate) appends `"<id> is on the route but the day runs out before it — try moving it to another day on the map"` to `warnings`; spots already reported as stuck on the planner side are not reported twice (deduplicated by id). A dot on the map only means "on the route" — whether a visit actually happened lives in the schedule and the warnings.

### Edge-case suite results (2026-08-28)

12 cases ran green: all 7 invalid-input 400 frozen messages correct; Disney-solo / `none`-`none` two-day / `few`-`few` with flight times / three-city custom all passed; `transit-legs` with empty `pairs` returned 400 and a single live pair worked. The baseline scenario once showed "Senso-ji planned but never visited, with no warning" — exactly the unvisited warning added here. ~~Known unfixed: middle days can still push the last spot past closing because capacity uses the 25-minutes-per-hop approximation~~ — now covered by the T-045 terminal check (approximate-clock replay plus a conservative arrival-day first leg; an extreme 17:00-flight itinerary produced zero warnings in production on 2026-08-28). The residual risk is only the extreme combination where a middle day's real transit vastly exceeds 25 minutes per hop — post-hackathon list.

### Closing-time terminal check and hours parsing (T-045, shipped)

- **`parse_hours` extended:** beyond Google style (`Open · Closes 5 PM`), plain ranges `9:00-17:00` / `9 AM - 5 PM` / `Mon 9:00-17:00; Tue closed` (first segment) all parse; when neither side carries AM/PM they are read by convention (`9-5` = 9 a.m. to 5 p.m.). Audit: 0 parse failures across all 328 catalog entries. **A closing time it cannot parse means the spot is treated as always open** — window feasibility, earliest-closing-first ordering, clock clipping, and the terminal check all depend on it, so changing it requires rerunning the full-catalog audit.
- **`_pack_day_capacity` budget guard:** before moving a spot, check that the target day can absorb it (including its own budget). Previously only load was compared, so the arrival day — budget 365, lightest of all — became an overflow dump for spots that would already be closed by the time the traveller arrived.
- **`_enforce_closing`, rescue layers** (runs after both branches converge, hotels already fixed): in-day reorder → cross-day move → cross-day swap (budget may not worsen; the arrival day is already over its target budget) → only then `warnings`. **`_closed_at_arrival`** is its simulator: hops use a distance formula calibrated on 163 real cached queries (`15 + 3.5 × km`, capped at 90 — the flat 25-minute hop under-measured Tokyo, where 6–10 km legs median 41 min), lunch (+60) is only added when a visit actually crosses the 11:30–14:00 window, and only a parsed closing time can veto a slot.
- **Arrival-day hotel-proximity anchoring (T-051b)**: arrival-day spots more than 5 km from that night's hotel are exiled to the geographically-nearest feasible day (an emptied arrival day pulls the nearest within-5km spot back in). DeepSeek's prompt carries the same constraint. Rationale: after landing + check-in, sending the traveller 8 km across the city twice is the single worst-reviewed behaviour this product produced.
- **Conservative arrival-day first leg:** at planner time the real legs do not exist yet (they are queried in step 4), so 25 minutes per hop let a "phantom 16:20 arrival at Ueno Park" through when reality was 18:40. The terminal check's start clock became landing + 90 + 20 min check-in + rail at 1.2 minutes per kilometre (Narita, 60 km ≈ 92 min; measured 99 — close). Better to move a spot to another day (harmless) than to leave a would-be-closed spot on the arrival day.
- **DeepSeek prompt synced:** the arrival-day rule adds "the night's last stop must still be open — earlier-closing museums first, nightlife districts last".

### Greedy NN-chain geographic grouping (T-052/T-053, shipped)

Replaces `RuleBasedGrouper` for the rule path and as the DeepSeek fallback:

- From the city centroid, repeatedly walk to the nearest unassigned spot —
  producing a single geographically contiguous chain.
- Cut the chain into `n_days` contiguous segments (equal spot count, ±1).
  Each segment = one day; days are geographically contiguous by construction
  (no cross-district zigzag possible).
- **Anchor-follow repair**: if DeepSeek's table omits spots, each missing
  spot joins the day holding its geographically closest placed spot (≤ 8 km),
  keeping far-suburb siblings together instead of scattering them.

Replaces the previous approach where `RuleBasedGrouper` clustered by district
then DeepSeek could scatter them randomly. The NN chain is deterministic —
the same spots always produce the same grouping.

### Geography-first destination days (T-054, shipped)

The NN chain builds contiguous days, but three downstream stages move spots
across days — capacity packing, opening-window relocation, and the closing-time
rescues — and all three used to pick the destination by **minimum load alone**,
quietly scattering spots across town after the grouping layers had done their
job (prod 2026-09-03: Arashiyama Monkey Park on a Sannenzaka day while the
rest of Arashiyama sat on another). Fixes:

- Every destination choice now prefers the **geographically nearest day**
  (distance from the spot to the day's centroid), with load as tie-break.
  Closing feasibility and budget remain hard filters — geography only breaks
  ties among days that are actually allowed.
- **Final-state district police**: after the closing-time check, the grouping
  is re-checked for district splits (`_repair_districts` + `_districts_ok`);
  residuals are repaired and the closing check re-runs so the repaired state
  meets the same feasibility bar. This catches evictions where the only
  budget-feasible day was across town.
- The district-outlier gate tightens from 8 km to **4 km**: on a 3-spot day a
  far spot drags its own centroid toward itself (monkey park sat ~7 km from
  its own day's centroid and escaped the old gate). The 2 km closer-other-day
  margin is unchanged, so boundary spots on contiguous chains don't churn.
- Closing-check moves are now logged (they used to be silent and
  undiagnosable in production).

Verified: the Kyoto scenario (5 days, 11 spots, KIX arrival) run 5× puts the
whole Arashiyama cluster on one day with the monkey park first (it closes at
16:00), east-side spots (Sannenzaka + Kiyomizu-dera) on another, zero
warnings every run; Osaka/Tokyo rule-path and two-city regressions unchanged.

### Group spots by district, never by count alone (shipped)

A sensible Shanghai split, for example: day 1 the Bund + Lujiazui + Shanghai Tower (one riverside district); day 2 Disney alone; day 3 Yu Garden + Xintiandi (old town / Huaihai) — not Shanghai Tower crammed back into day 3.

The rule path clusters by geographic district first (nearby clusters merged) and no longer splits districts to keep per-day counts within 1 of each other. A successful DeepSeek table is validated against the same district rule: repair first (move the clearly misplaced spots), and only if repair cannot fix it does the whole city fall back to rules.

### Capacity counts stays and meals, not just spot counts (capacity shipped; clock times clipped to hours)

Parks with `suggested_duration_min` ≥ 360 already take a whole day. Lunch (1 h) and dinner (1.5 h) count toward "does this fit in the day". Catalog `opening_hours` goes into the DeepSeek prompt; `schedule.py` and the result page clip visits to opening and closing times; and no dinner is added once the departure hub has been reached.

---

## Known limitations

- **`parse_hours` is a single point of failure for time awareness.** A closing time it cannot parse is treated as always open, and window feasibility, earliest-closing-first ordering, clock clipping, and the terminal check all sit on top of it. Any change requires the full-catalog audit (328 entries, 0 failures as of 2026-08-28).
- **Capacity still estimates hops by distance** (the same calibrated formula the terminal check uses), never by live timetables — real transit is only queried for display after the plan ships. The residual risk — a middle day whose real transit vastly exceeds the estimate — is on the post-hackathon list.
- **`few` is best-effort, not a guarantee.** It means "as few as possible", and a city with no free middle days (or only one day at all) can still carry many spots on its `few` day.
- **Capacity is an estimate, not a timetable.** Only the closing-time terminal check replays realistic clock times, and it too models the arrival-day first leg from distance rather than from a real transit answer.
- **No real departure dates are collected yet.** Transit queries use synthetic "Nth day from tomorrow" dates so SerpApi receives a future moment; cache keys bucket by hour only, so a cached answer for "09:00" is reused regardless of the calendar day.
