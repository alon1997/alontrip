# AlonTrip API

This page follows the code. Change a route in `backend/app/main.py` first, then change this page. Interactive docs on a local machine: `http://127.0.0.1:5003/docs`.

Everything lives under the `/api/trip` prefix. Errors come back as `{"detail":"english reason"}`. External ids are slugs (`senso-ji`).

Secrets live in the repo-root `.env` (see `.env.example`), read by `backend/app/config.py`.

**`main.py` is not the algorithm.** It is the front desk: the browser hits a URL, and `main.py` decides who does the work and assembles the result into JSON. Route planning (which day goes to which city, where to sleep) lives in `plan_trip()` in `backend/app/services/planner.py`.

---

## Who gets called: SerpApi / DeepSeek

| External service | Environment variable | What it does |
|----------|----------|--------|
| SerpApi | `SERPAPI_KEY` | Japan / Korea / Hong Kong public transit; POI and lodging search when the catalog misses. Amounts converted to USD |
| AMap | `AMAP_KEY` | Mainland China public transit. Without a key, mainland legs only get a taxi estimate |
| DeepSeek | `DEEPSEEK_API_KEY` | Splits POIs across days. Never queries transit. Model `deepseek-v4-flash`, thinking disabled on the request (constants live in `grouping.py`, shared with `planner.py`) |

The app also runs with no keys at all: transit uses cache/estimates, grouping uses rules. The browser calls **our own** `/api/trip/...`, never serpapi.com directly. A missing key must not crash the process (D-010): without SerpApi it reads `data/transit_cache/` or estimates straight-line; without DeepSeek it groups by rules. With no keys the process **never calls** `serpapi.com` or `api.deepseek.com`.

| Endpoint | SerpApi | DeepSeek |
|------|:-------:|:--------:|
| `GET /health` `/cities` `/pois` `/lodgings` `/transport-hubs` | no | no |
| `GET /pois/search` `/lodgings/search` | only on a catalog miss | no |
| `POST /optimize-route` | one call per leg (skipped on a `data/transit_cache/` hit) | groups when a key exists; falls back to rules on failure |
| `POST /transit-legs` | uncached legs only | no |

Ticking catalog POIs or letting the system pick hotels never calls SerpApi.

Every transit leg carries `route.data_source`: `serpapi_live` (called serpapi.com this time) / `serpapi_cache` (replaying a stored SerpApi response) / `walk` / `taxi` / `estimate` / `amap`. Old cache files without the field are read as `serpapi_cache`.

---

## Endpoint overview

**`GET /health`** — liveness. `transit_provider` / `grouper` are the implementations the process picked at startup, not proof that the last call succeeded. `serpapi_configured` / `deepseek_configured` only mean the environment variables are non-empty; secrets are **never returned**.

**`GET /cities`** — the nine cities plus `countries` (Japan / China / Korea).

**`GET /pois?city=`** · **`GET /lodgings?city=`** · **`GET /transport-hubs?city=`** — catalogs, no external API. Unknown city → 400. POIs carry `requires_ticket`, `ticket_price`, `ticket_currency` (**USD**), `opening_hours`, `suggested_duration_min`.

**`GET /pois/search?city=&q=`** · **`GET /lodgings/search`** — `q` must be at least 2 characters. `source`: `db` | `local-json` | `serpapi`.

**`POST /optimize-route`** — the planner page's Generate. 10-second cooldown per IP.

| Field | Meaning |
|------|------|
| `cities[]` | 1–4 cities, in travel order |
| `days` | ≥ number of cities |
| `poi_ids[]` | at least one per city |
| `hotel_mode` | `system_one` / `system_multi` / `custom` |
| `arrival_hub_id` / `departure_hub_id` | arrival hub of the first city / departure hub of the last city |
| `arrival_time` / `departure_time` (T-034) | `HH:MM`, 24-hour, interpreted in the hub city's local time; optional; must be submitted together with the matching hub — an orphan time is a 400 (`arrival_time requires arrival_hub_id`), a malformed value is a 400 (`arrival_time must be HH:MM (24h)`). Landing buffer 90 minutes, takeoff buffer 120 minutes (see docs/ALGORITHM.md for the buffers) |
| `first_day_density` / `last_day_density` | each `none` (zero spots if possible) or `few` (as few as possible, **not** a hard cap of 1). All four combinations are valid; defaults are few / none |
| `edge_density` | legacy packing field; used only when neither of the two switches above is sent. Includes `first_none_last_none` |

The legacy singular `city` field still works (treated as one city + `system_one`).

**`POST /transit-legs`** — result-page fine-tuning. At most 30 pairs. Never runs DeepSeek.

---

## Frozen error strings

Validation failures return these exact English strings. The frontend matches them verbatim, so the wording must not be rephrased. `PlanningError` raised inside `planner.py` is re-raised by `main.py` as a 400 with the same `detail`.

### 400 from `main.py`

| `detail` | Raised when |
|------|------|
| `unknown city` | city is not one of the nine |
| `query too short` | search term is shorter than 2 characters after trimming |
| `at least one city is required` | `cities[]` is empty |
| `arrival_time requires arrival_hub_id` | orphan arrival time (no matching hub) |
| `departure_time requires departure_hub_id` | orphan departure time (no matching hub) |
| `arrival_time must be HH:MM (24h)` / `departure_time must be HH:MM (24h)` | malformed time (`{field} must be HH:MM (24h)`) |
| `pairs must not be empty` | `/transit-legs` with no pairs |
| `too many pairs` | `/transit-legs` with more than 30 pairs |
| `unknown kind: {kind}` | pair node kind is not `poi` / `lodging` / `hub` |
| `lat and lng are required` | pair node missing coordinates |

### 400 from `planner.py` (`PlanningError`)

| `detail` | Raised when |
|------|------|
| `at most 4 cities` | more than 4 cities |
| `at least one city is required` | no city |
| `days must be >= number of cities` | fewer days than cities |
| `days must be between 2 and 14` | `days` out of range |
| `hotel_mode must be one of system_multi, system_one, custom` | bad hotel mode |
| `first_day_density and last_day_density must be none or few` | bad edge density switch |
| `edge_density must be one of [...]` | legacy field outside the allowed set (includes `first_none_last_none`) |
| `unknown arrival_hub_id: {id}` / `unknown departure_hub_id: {id}` | hub not in that city's catalog (`unknown {role}_hub_id: {hub_id}`) |
| `unknown poi_ids: {ids}` | a selected POI is not in the catalog |
| `each city must have at least one selected poi` | a city with zero selected spots |
| `custom_stays must cover every day` | custom lodging missing a day |
| `unknown lodging_id: {id}` | custom stay points at a lodging outside the city |
| `no lodgings available for city '{city}'` | city has no catalog lodging |

The only non-400 guard is the Generate cooldown: `429` with `detail: "slow down"` when the same IP calls `/optimize-route` twice within 10 seconds.

Warnings are frozen strings too (not errors): `"{poi_id} cannot fit within opening hours on its assigned day"` and `"{poi_id} is on the route but the day runs out before it — try moving it to another day on the map"` (see docs/ALGORITHM.md).

---

## `main.py` walkthrough (read it like an operator)

Path: `backend/app/main.py`. Line numbers drift as the file changes; find things by **name**, do not memorize line numbers.

### What this file does and does not do

Does: recognize cities, block invalid requests, enforce the 10-second cooldown, load catalogs into memory, call `plan_trip`, query transit for adjacent pairs, and stitch each day into JSON.

Does not: decide "Asakusa or Shibuya on which day" (that is `planner.py`); turn coordinates into subway line names (that is `transit.py`); search for new place names (that is `search.py`).

Run locally (from `backend/`):

```bash
uvicorn app.main:app --reload --port 5003
python -m app.main          # reads APP_PORT, defaults to 5003
```

The `if __name__ == "__main__"` block at the end binds `127.0.0.1` only. In production Nginx reverse-proxies to it and the port is not exposed.

### Top-of-file constants

| Name | Purpose |
|------|--------|
| `CITIES` | Identity cards for the nine cities: `id` (`tokyo`), Chinese name, English name, `country` (japan/china/korea). One authoritative source for the frontend city list. |
| `CITY_IDS` | Set of the nine ids above. A request for Paris → 400 `unknown city`. |
| `COUNTRY_NAMES` | `japan` → `Japan`, etc. Grouping labels for the frontend, in English. |
| `CITY_TIMEZONES` | Which timezone each city's transit queries run in. Keeps a Shanghai-hosted clock from asking the Tokyo subway. |
| `INTRA_CITY_HOUR = 9` | Intra-city legs: depart 9 a.m. local. |
| `INTERCITY_HOUR = 16` | City-change legs: depart 4 p.m. local (mornings stay free for sightseeing). |

The product does not collect real departure dates yet. Transit queries use "Nth day from tomorrow" as the date purely so SerpApi receives a future moment; the cache key buckets by hour only (`09` / `16`), with no calendar day in it.

### Small helpers (leading underscore = file-private)

**`_depart_at(timezone, day, hour)`**
Turns "day N + hour + timezone" into a Unix timestamp for SerpApi's `depart_at`.

**`_timezone_for_lng(lng)`**
Result-page fine-tuning supplies coordinates only, no city name. Longitude &lt; 124 → China/Hong Kong (UTC+8); otherwise Japan/Korea (UTC+9).

**`_query_leg(...)`**
Queries **one** leg, shared by `optimize-route` and `transit-legs`. Intra-city uses 9 a.m., city-change uses 4 p.m., then asks the transit layer (with a key: SerpApi + cache; straight-line under 800 m counts as walking). A failure never sinks the whole request: it logs one warning line and falls back to the estimate route (road-ish distance at taxi speed, USD fare from a flag plus a per-km rate), and the page marks the leg `estimated=true`.

**`_check_cooldown`**
The same IP cannot hit Generate twice within 10 seconds. `_last_optimize_call` lives in **process memory** and resets on restart. The fine-tuning endpoint is not throttled; it stays cheap through the cache.

**`_require_known_city` / `_require_query`**
The city must be one of the nine; the search term must be at least 2 characters after trimming whitespace.

### Startup `lifespan`

When the process boots it first calls the factory: one implementation each for POIs, lodgings, transit, and grouping, each logged. The terminal shows `Transit provider: serpapi` or `local-json`. Choices are cached in the process (`lru_cache`), not re-picked per request.

Then `FastAPI(...)` + CORS: by default the local Vite `5173` and preview `4173` ports are allowed, overridable with `CORS_ORIGINS`.

### The `class xxx(BaseModel)` blocks = parcel specifications

Pydantic models: the JSON the frontend sends must look like this, or the backend cannot unpack it. Field constraints are deliberately loose so rejection goes through `PlanningError`'s fixed English `detail` instead of FastAPI's default 422.

| Class | Used by |
|----|------|
| `CustomStayBody` | custom lodging: which hotel, which days it covers |
| `OptimizeRequest` | the whole Generate parcel. `city` (singular) is the legacy field: sending only it means `cities=[city]` + `hotel_mode=system_one` |
| `Node` / `Leg` | nodes and legs returned to the frontend |
| `LegNode` / `LegPairRequest` / `TransitLegsRequest` | fine-tuning: at most 30 pairs; `kind` must be `poi` / `lodging` / `hub`; missing coordinates get our own 400, not a 422 |

### Each `@app.get` / `@app.post` is one endpoint

The path in the decorator is the URL the browser calls (the `/api/trip` prefix is already included).

**`GET /health`**
Still alive? Returns the currently selected implementations: `poi_provider`, `lodging_provider`, `transit_provider`, `grouper`, plus `serpapi_configured` / `deepseek_configured` (non-empty environment variables, no secrets). With a DeepSeek key present, `grouper` may say deepseek at boot — that does **not** mean the last Generate actually reached the model. Whether it was really used shows in the `grouper` field of the `optimize-route` response.

**`GET /cities`**
The nine cities + per-city catalog POI counts + `countries`. No external API.

**`GET /pois` · `/lodgings` · `/transport-hubs`**
Returns the list for `?city=`. Unknown city → 400. No web search, no itinerary. Ticking catalog spots costs no SerpApi quota.

**`GET /pois/search` · `/lodgings/search`**
`?city=&q=`. Catalog first (MySQL or JSON); on a hit `source` is `db` / `local-json`. Only on a miss, and only with `SERPAPI_KEY` set, does it call SerpApi `google_maps` (that is SerpApi's engine name, not Google's official API). No hit and no key: 200 with an empty list. A found result that has a database connection is written back with `source=search`, so the next lookup hits the catalog.

**`POST /optimize-route` (Generate)**

1. `_check_cooldown`.
2. `_resolve_cities_and_hotel_mode`: read the city list and lodging mode.
3. Load those cities' POIs, hostels, and stations from the catalog into memory.
4. Call **`plan_trip(...)`** (`planner.py`): decide "which day goes to which city, where to sleep". With `DEEPSEEK_API_KEY` set it fills each city with the model; a failure falls back to rules for that city only. Validation failure → 400 with a frozen English `detail`.
5. `_build_day_chain`: one chain per day. Default is "morning hotel → POIs… → that night's hotel". On day 1 with an arrival hub and at least one POI: `hub → hotel(checkin) → POIs… → that night's hotel` (T-024); with no POIs, hub → hotel. On the last day with a departure hub, the chain ends at the station/airport.
6. `_query_leg` for each adjacent pair (SerpApi / cache / walk / estimate). Only adjacent pairs are queried, not the full POI-by-POI matrix. Intercity is just one of these legs; if Google folds a Shinkansen ride into Japan's domestic transit results, it shows up as-is — there is no separate JR integration.
7. `build_day_schedule` (`schedule.py`): accumulate clock times from the chain and the transit results. Starts 09:00; an intercity leg still in the morning jumps to 16:00; lunch 60 minutes, dinner 90 minutes; day-1 check-in about 20 minutes. Stays use `suggested_duration_min`. This is the display layer; it never changes `plan_trip`'s day assignment.
8. On the departure day, when the chain ends at a hub, that night's hotel is reported as the morning one, because the chain's tail hotel is not really checked into that day.
9. Returns `itinerary` (each day with `schedule`) + `totals` + `first_day_density` / `last_day_density`. `grouper`: `deepseek` / `rule-based` / `mixed`. `all_real_data`: whether every leg avoided estimates. T-034: also returns `arrival_start_min` / `departure_cutoff_min` (minute-of-day values, nullable; the frontend's schedule mirror uses them). When a POI cannot open inside its window and cannot be moved, `warnings` gets `"<poi_id> cannot fit within opening hours on its assigned day"`. T-040: `totals` carries `transit_cost` + `ticket_cost` (only visits that really happen) + `lodging_cost` (nights actually spent; the last night is not counted on the departure-hub day) + `grand_total` + `unknown_prices` (count of priceless items, billed as $0); each day's `itinerary[].ticket_cost` is returned alongside.

**`POST /transit-legs` (result-page fine-tuning)**

Re-queries only the adjacent pairs that changed. Same `_query_leg`. No `plan_trip`, no DeepSeek, no 10-second cooldown. Empty `pairs` or more than 30 → 400.

---

## Where the planning algorithm lives

Discuss the algorithm through the prose version [ALGORITHM.md](ALGORITHM.md) (it tracks `planner.py`); do not reverse-engineer it from source.

| File | Purpose |
|------|--------|
| **`backend/app/services/planner.py`** | **The main algorithm.** `plan_trip()`: split days across cities, assign POIs per city, pick hotels, assemble each day's nodes. With a key, `_deepseek_fill_city()` calls DeepSeek per city. |
| `backend/app/services/grouping.py` | Rule-based fallback when DeepSeek fails (geographic clustering + roughly even spots per day + nearest-neighbor ordering). `DEEPSEEK_MODEL` is defined here. |
| `backend/app/services/schedule.py` | The result page's clock-time schedule (lunch/dinner/check-in). Never changes the day assignment. |
| `backend/app/services/transit.py` | Does no scheduling. How to get between two points (SerpApi / cache / walk / straight-line estimate). |
| `backend/app/main.py` | The operator. See the previous section. |

The `grouper` in `GET /health` comes from `factory.py`'s `LLMGrouper`; **the itinerary is actually filled** by `planner.py`, not by the boot-time `get_grouper().group()`.

Visit durations (`suggested_duration_min`) are audited into the catalog JSON. They drive both the result-page timeline and day capacity: `_pack_day_capacity` packs a day from stays + lunch + dinner + a per-hop transit estimate. Opening hours are in the algorithm too — `parse_hours` feeds window feasibility (`_move_infeasible_for_windows`), early-closing ordering, and the closing-time terminal check (`_enforce_closing`). See docs/ALGORITHM.md.

---

## Files to touch when changing an endpoint

| Layer | File |
|----|------|
| Routes / wiring | `backend/app/main.py` |
| Environment variables | `backend/app/config.py` |
| Key-dependent implementation choice | `backend/app/services/factory.py` |
| Itinerary / DeepSeek | `backend/app/services/planner.py`, `grouping.py` |
| Clock-time schedule | `backend/app/services/schedule.py` |
| SerpApi transit | `backend/app/services/transit.py` |
| SerpApi search | `backend/app/services/search.py` |
