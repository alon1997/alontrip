"""FastAPI entry point.

Run locally (from the backend/ directory):

    uvicorn app.main:app --reload --port 5003   # dev
    python -m app.main                          # uses APP_PORT (default 5003)

Works with zero API keys (D-010): without SERPAPI_KEY / DEEPSEEK_API_KEY the
local JSON and rule-based implementations are selected automatically.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import get_cors_origins, get_settings
from .services.currency import to_usd
from .services.factory import (
    get_grouper,
    get_lodging_provider,
    get_poi_provider,
    get_transit_provider,
    get_transport_hub_provider,
)
from .services.planner import (
    CustomStayInput,
    PlanningError,
    plan_trip,
    resolve_edge_modes,
)
from .services.schedule import (
    LANDING_BUFFER_MIN,
    TAKEOFF_BUFFER_MIN,
    build_day_schedule,
)
from .services.search import search_lodgings, search_pois
from .services.transit import Coord, TransitRoute, estimate_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# T-016: ``country`` groups the city picker on the frontend (Japan/China/Korea).
CITIES = [
    {"id": "tokyo", "name": "Tokyo", "name_en": "Tokyo", "country": "japan"},
    {"id": "osaka", "name": "Osaka", "name_en": "Osaka", "country": "japan"},
    {"id": "kyoto", "name": "Kyoto", "name_en": "Kyoto", "country": "japan"},
    {"id": "beijing", "name": "Beijing", "name_en": "Beijing", "country": "china"},
    {"id": "shanghai", "name": "Shanghai", "name_en": "Shanghai", "country": "china"},
    {"id": "hongkong", "name": "Hong Kong", "name_en": "Hong Kong", "country": "china"},
    {"id": "seoul", "name": "Seoul", "name_en": "Seoul", "country": "korea"},
    {"id": "busan", "name": "Busan", "name_en": "Busan", "country": "korea"},
    {"id": "jeju", "name": "Jeju", "name_en": "Jeju", "country": "korea"},
]
CITY_IDS = {city["id"] for city in CITIES}
COUNTRY_NAMES = {"japan": "Japan", "china": "China", "korea": "Korea"}

# T-012 / plan 7.3: query transit at a plausible local time, not server time.
CITY_TIMEZONES = {
    "tokyo": "Asia/Tokyo", "kyoto": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "busan": "Asia/Seoul", "jeju": "Asia/Seoul",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "hongkong": "Asia/Hong_Kong",
}
INTRA_CITY_HOUR = 9   # within-city legs: depart 09:00 local
INTERCITY_HOUR = 16   # city-to-city legs: depart 16:00 local (morning left free)


def _depart_at(tz_name: str, trip_day: int, hour: int) -> int:
    """Unix timestamp for ``hour`` local time in timezone ``tz_name`` on the
    trip's ``trip_day``-th day (day 1 == tomorrow — no real trip start date
    is collected yet, so "starts tomorrow" keeps every query in the future).
    Only the hour feeds the cache key (see ``hour_bucket``); the exact
    calendar date just needs to be a real, near-future date in the right
    timezone so SerpApi returns a live schedule instead of erroring out.
    """
    tz = ZoneInfo(tz_name)
    anchor_date = date.today() + timedelta(days=trip_day)
    local_dt = datetime.combine(anchor_date, dt_time(hour=hour), tzinfo=tz)
    return int(local_dt.timestamp())


def _timezone_for_lng(lng: float) -> str:
    """``/transit-legs`` pairs carry coordinates but no city id. Every
    supported city falls into one of two offsets — mainland China/HK at
    UTC+8 (lng < 124, e.g. Shanghai 121.5, Beijing 116.4, Hong Kong 114.1)
    versus Japan/Korea at UTC+9 with no DST in either (lng >= 124, e.g.
    Seoul 127.0, Tokyo 139.7) — so a longitude split is enough to avoid
    hardcoding Shanghai server time (plan 7.3) without needing a city field.
    """
    return "Asia/Shanghai" if lng < 124 else "Asia/Tokyo"


def _query_leg(
    transit, origin: Coord, dest: Coord, *, tz_name: str, intercity: bool, trip_day: int = 1, city: str = "",
    hour_override: int | None = None,
) -> TransitRoute:
    """Shared by ``/optimize-route`` and ``/transit-legs`` (T-013): same
    time-of-day rule, same cache, same per-leg estimate fallback so a single
    bad SerpApi call never fails the whole request. ``hour_override`` (T-034)
    replaces the 09:00 default on arrival-day legs so the queried schedule
    matches the post-landing start instead of a morning that never existed."""
    hour = hour_override if hour_override is not None else (INTERCITY_HOUR if intercity else INTRA_CITY_HOUR)
    hour_bucket = f"{hour:02d}"
    depart_at = _depart_at(tz_name, trip_day, hour)
    try:
        return transit.get_route(
            origin, dest, depart_at=depart_at, hour_bucket=hour_bucket, city=city,
        )
    except Exception as exc:  # noqa: BLE001 — one bad leg must not sink the whole request
        logger.warning("transit provider failed for (%s,%s) -> (%s,%s): %s", origin.lat, origin.lng, dest.lat, dest.lng, exc)
        return estimate_route(origin, dest, city=city)


# T-013: same-IP cooldown on /optimize-route only — /transit-legs is used for
# rapid map fine-tuning and must not be throttled, it just relies on the
# transit cache to stay cheap.
OPTIMIZE_COOLDOWN_S = 10.0
_last_optimize_call: dict[str, float] = {}


def _check_cooldown(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    last = _last_optimize_call.get(client_ip)
    if last is not None and now - last < OPTIMIZE_COOLDOWN_S:
        raise HTTPException(status_code=429, detail="slow down")
    _last_optimize_call[client_ip] = now


def _require_known_city(city: str) -> None:
    if city not in CITY_IDS:
        raise HTTPException(status_code=400, detail="unknown city")


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(value: str | None, field: str) -> int | None:
    """``"14:30"`` → 870 minutes from midnight (T-034). ``None``/``""`` → None."""
    if value is None or not value.strip():
        return None
    match = _HHMM.match(value.strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise HTTPException(status_code=400, detail=f"{field} must be HH:MM (24h)")
    return int(match.group(1)) * 60 + int(match.group(2))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Resolve all providers at startup so the active data sources are logged
    # exactly once and are grep-able from the startup output.
    get_poi_provider()
    get_lodging_provider()
    get_transit_provider()
    get_grouper()
    yield


app = FastAPI(title="AlonTrip API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),  # CORS_ORIGINS env var; defaults to dev + preview
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CustomStayBody(BaseModel):
    lodging_id: str
    days: list[int]


class OptimizeRequest(BaseModel):
    """New contract (implementation batches doc 2.6). ``city`` (singular) is the pre-T-010
    legacy field: bodies that send only ``city`` are treated as
    ``cities=[city]``, ``hotel_mode="system_one"`` — see the endpoint.
    Field-level constraints are deliberately loose here so every rejection
    goes through :class:`PlanningError` and returns the frozen 400 `detail`
    strings, not FastAPI's default 422 shape.
    """

    cities: list[str] | None = None
    city: str | None = None  # legacy
    days: int
    poi_ids: list[str] = Field(default_factory=list)
    hotel_mode: str = "system_multi"
    custom_stays: list[CustomStayBody] = Field(default_factory=list)
    # T-016: which airport/station the traveller arrives at / leaves from —
    # arrival must belong to the first city in ``cities``, departure to the
    # last. Optional: omitting either keeps the pre-T-016 hotel-only chain.
    arrival_hub_id: str | None = None
    departure_hub_id: str | None = None
    # T-034: flight landing / takeoff time as HH:MM (24h), local to the
    # matching hub's city. Optional; must be sent together with the hub id.
    arrival_time: str | None = None
    departure_time: str | None = None
    first_day_density: str | None = None
    last_day_density: str | None = None
    # Packed T-016 field; used only when the two switches above are omitted.
    edge_density: str | None = None


class Node(BaseModel):
    kind: str  # "lodging" | "poi" | "hub"
    id: str
    role: str | None = None  # lodging: "start" | "end" | "checkin"; hubs use start/end


class Leg(BaseModel):
    from_id: str
    from_kind: str
    to_id: str
    to_kind: str
    intercity: bool
    route: TransitRoute


class LegNode(BaseModel):
    """``lat``/``lng`` are optional here (not on :class:`Node`) purely so a
    missing coordinate produces our own 400 ``detail`` (T-013) instead of
    FastAPI's default 422 shape."""

    id: str
    kind: str
    lat: float | None = None
    lng: float | None = None


class LegPairRequest(BaseModel):
    from_: LegNode = Field(alias="from")
    to: LegNode
    intercity: bool = False


class TransitLegsRequest(BaseModel):
    pairs: list[LegPairRequest] = Field(default_factory=list)


MAX_TRANSIT_LEGS_PAIRS = 30
VALID_NODE_KINDS = {"poi", "lodging", "hub"}


def _configured(value: str | None) -> bool:
    return bool(value and value.strip())


@app.get("/api/trip/health")
def health() -> dict[str, object]:
    """Liveness probe; also reports which data sources are active."""
    settings = get_settings()
    return {
        "status": "ok",
        "poi_provider": get_poi_provider().name,
        "lodging_provider": get_lodging_provider().name,
        "transit_provider": get_transit_provider().name,
        "grouper": get_grouper().name,
        "serpapi_configured": _configured(settings.serpapi_key),
        "deepseek_configured": _configured(settings.deepseek_api_key),
    }


@app.get("/api/trip/cities")
def cities() -> dict[str, object]:
    """Supported cities with POI counts, from whichever provider is active.
    ``country`` (T-016) lets the frontend group the picker by Japan/China/Korea
    without hardcoding the city->country map on its own."""
    provider = get_poi_provider()
    return {
        "cities": [
            {**city, "poi_count": len(provider.get_pois(city["id"]))} for city in CITIES
        ],
        "countries": [{"id": cid, "name": name} for cid, name in COUNTRY_NAMES.items()],
    }


@app.get("/api/trip/transport-hubs")
def transport_hubs(city: str) -> dict[str, object]:
    """Airports + intercity train/metro stations for ``city`` (T-016) — the
    traveller picks one as the arrival anchor for their first city and one
    as the departure anchor for their last."""
    _require_known_city(city)
    items = get_transport_hub_provider().get_hubs(city)
    return {"city": city, "count": len(items), "hubs": items}


@app.get("/api/trip/pois")
def pois(city: str) -> dict[str, object]:
    _require_known_city(city)
    items = get_poi_provider().get_pois(city)
    return {"city": city, "count": len(items), "pois": items}


@app.get("/api/trip/lodgings")
def lodgings(city: str) -> dict[str, object]:
    """Default catalog (listed=1) hostels/capsules for the given city."""
    _require_known_city(city)
    items = get_lodging_provider().get_lodgings(city)
    return {"city": city, "count": len(items), "lodgings": items}


def _require_query(q: str) -> str:
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="query too short")
    return q


@app.get("/api/trip/pois/search")
def pois_search(city: str, q: str) -> dict[str, object]:
    """Catalog-first POI search (T-009): checks mysql/local-json before ever
    calling SerpApi. See app/services/search.py for the full contract."""
    _require_known_city(city)
    query = _require_query(q)
    source, results = search_pois(city, query, get_settings())
    return {"city": city, "query": query, "source": source, "results": results}


@app.get("/api/trip/lodgings/search")
def lodgings_search(city: str, q: str) -> dict[str, object]:
    """Catalog-first lodging search (T-009), same discipline as /pois/search."""
    _require_known_city(city)
    query = _require_query(q)
    source, results = search_lodgings(city, query, get_settings())
    return {"city": city, "query": query, "source": source, "results": results}


def _resolve_cities_and_hotel_mode(req: OptimizeRequest) -> tuple[list[str], str]:
    if req.cities:
        cities = list(dict.fromkeys(req.cities))  # dedupe, keep first-seen order
        return cities, req.hotel_mode
    if req.city:
        return [req.city], "system_one"
    raise HTTPException(status_code=400, detail="at least one city is required")


def _build_day_chain(
    planned_day, total_days: int, arrival_hub, departure_hub
) -> list[tuple[str, str, float, float, str | None, str]]:
    """(kind, id, lat, lng, role, city) per node.

    Day 1 with an arrival hub AND at least one POI (T-024): hub → hotel
    check-in → POIs → night hotel (or departure hub on a 1-day trip).
    Without POIs, hub → hotel is enough (drop bags / sleep).
    """
    morning = (
        "lodging", planned_day.morning_lodging.id,
        planned_day.morning_lodging.lat, planned_day.morning_lodging.lng,
        "start", planned_day.morning_lodging.city,
    )
    night = (
        "lodging", planned_day.lodging.id,
        planned_day.lodging.lat, planned_day.lodging.lng,
        "end", planned_day.lodging.city,
    )
    poi_nodes = [("poi", poi.id, poi.lat, poi.lng, None, poi.city) for poi in planned_day.pois]
    use_arrival = planned_day.day == 1 and arrival_hub is not None
    use_departure = planned_day.day == total_days and departure_hub is not None

    if use_arrival:
        hub_start = ("hub", arrival_hub.id, arrival_hub.lat, arrival_hub.lng, "start", arrival_hub.city)
        checkin = (
            "lodging", planned_day.morning_lodging.id,
            planned_day.morning_lodging.lat, planned_day.morning_lodging.lng,
            "checkin", planned_day.morning_lodging.city,
        )
        if poi_nodes:
            chain = [hub_start, checkin, *poi_nodes]
        else:
            chain = [hub_start]
    else:
        chain = [morning, *poi_nodes]

    if use_departure:
        chain.append(("hub", departure_hub.id, departure_hub.lat, departure_hub.lng, "end", departure_hub.city))
    else:
        chain.append(night)
    return chain


@app.post("/api/trip/optimize-route")
def optimize_route(req: OptimizeRequest, request: Request) -> dict[str, object]:
    _check_cooldown(request)
    cities, hotel_mode = _resolve_cities_and_hotel_mode(req)
    for city in cities:
        _require_known_city(city)

    poi_provider = get_poi_provider()
    lodging_provider = get_lodging_provider()
    hub_provider = get_transport_hub_provider()
    poi_catalog = {city: poi_provider.get_pois(city) for city in cities}
    lodging_catalog = {city: lodging_provider.get_lodgings(city) for city in cities}
    hub_catalog = {city: hub_provider.get_hubs(city) for city in cities}
    custom_stays = [CustomStayInput(lodging_id=cs.lodging_id, days=cs.days) for cs in req.custom_stays]

    settings = get_settings()
    # T-034: flight landing/takeoff times (HH:MM, hub-local). A time without
    # its hub is a client bug — reject rather than silently ignoring it.
    arrival_time_min = _parse_hhmm(req.arrival_time, "arrival_time")
    departure_time_min = _parse_hhmm(req.departure_time, "departure_time")
    if arrival_time_min is not None and not req.arrival_hub_id:
        raise HTTPException(status_code=400, detail="arrival_time requires arrival_hub_id")
    if departure_time_min is not None and not req.departure_hub_id:
        raise HTTPException(status_code=400, detail="departure_time requires departure_hub_id")
    arrival_start_min = arrival_time_min + LANDING_BUFFER_MIN if arrival_time_min is not None else None
    departure_cutoff_min = departure_time_min - TAKEOFF_BUFFER_MIN if departure_time_min is not None else None
    try:
        first_d = req.first_day_density
        last_d = req.last_day_density
        plan = plan_trip(
            cities=cities,
            days=req.days,
            poi_ids=req.poi_ids,
            hotel_mode=hotel_mode,
            custom_stays=custom_stays,
            poi_catalog=poi_catalog,
            lodging_catalog=lodging_catalog,
            deepseek_api_key=settings.deepseek_api_key,
            deepseek_base_url=settings.deepseek_base_url,
            first_day_density=first_d,
            last_day_density=last_d,
            edge_density=req.edge_density,
            arrival_hub_id=req.arrival_hub_id,
            departure_hub_id=req.departure_hub_id,
            hub_catalog=hub_catalog,
            arrival_time_min=arrival_time_min,
            departure_time_min=departure_time_min,
        )
        first_day_density, last_day_density = resolve_edge_modes(first_d, last_d, req.edge_density)
    except PlanningError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    transit = get_transit_provider()
    itinerary: list[dict[str, object]] = []
    # T-046: query the legs in parallel. Root cause of the 504s (2026-08-28
    # prod): when a new grouping produces brand-new pairs, serially fetching
    # 17+ SerpApi legs took ~51 s and hit Nginx's 60 s timeout. Legs are
    # independent of each other, so a thread pool turns "the sum of all legs"
    # into "the slowest single leg".
    leg_specs: list[dict[str, object]] = []
    day_plans: list[tuple[object, list]] = []
    for planned_day in plan.days:
        chain = _build_day_chain(planned_day, req.days, plan.arrival_hub, plan.departure_hub)
        day_plans.append((planned_day, chain))
        for (from_kind, from_id, from_lat, from_lng, _from_role, from_city), \
            (to_kind, to_id, to_lat, to_lng, _to_role, to_city) in zip(chain, chain[1:]):
            leg_specs.append({
                "day": planned_day.day,
                "from_id": from_id, "from_kind": from_kind,
                "to_id": to_id, "to_kind": to_kind,
                "origin": Coord(lat=from_lat, lng=from_lng),
                "dest": Coord(lat=to_lat, lng=to_lng),
                # T-034: on the arrival day the first legs leave around the
                # post-landing start, not 09:00 — query a schedule that matches.
                "hour_override": (
                    arrival_start_min // 60
                    if planned_day.day == 1 and arrival_start_min is not None
                    else None
                ),
                "tz_name": CITY_TIMEZONES.get(from_city, "Asia/Tokyo"),
                "intercity": from_city != to_city,
                "trip_day": planned_day.day,
                "city": from_city,
            })

    def _fetch(spec: dict[str, object]) -> TransitRoute:
        return _query_leg(
            transit, spec["origin"], spec["dest"],  # type: ignore[arg-type]
            tz_name=spec["tz_name"], intercity=bool(spec["intercity"]),
            trip_day=spec["trip_day"], city=spec["city"],           # type: ignore[arg-type]
            hour_override=spec["hour_override"],                    # type: ignore[arg-type]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        all_routes = list(pool.map(_fetch, leg_specs))
    routes_by_day: dict[int, list[tuple[dict[str, object], TransitRoute]]] = {}
    for spec, route in zip(leg_specs, all_routes):
        routes_by_day.setdefault(spec["day"], []).append((spec, route))  # type: ignore[union-attr]

    # Honest reporting (bug-hunt 2026-08-28): a spot that is on the plan but
    # never got a visit row (closed by the time the chain reaches it) must be
    # surfaced — the map dot alone would silently promise a visit that
    # schedule.py refused to fabricate. Planner-side stuck spots already have
    # their own warnings; match on id to avoid double-reporting.
    unvisited_warnings: list[str] = []
    for planned_day, chain in day_plans:
        legs: list[Leg] = [
            Leg(
                from_id=spec["from_id"], from_kind=spec["from_kind"],     # type: ignore[arg-type]
                to_id=spec["to_id"], to_kind=spec["to_kind"],             # type: ignore[arg-type]
                intercity=bool(spec["intercity"]),
                route=route,
            )
            for spec, route in routes_by_day.get(planned_day.day, [])
        ]

        # T-016: the last day's "night stay" is fictional once a departure
        # hub replaces the chain's end node — nobody actually checks into
        # ``planned_day.lodging`` that night, so report the hotel that's
        # really still in the chain (the one from the morning) instead.
        is_departure_day = planned_day.day == req.days and plan.departure_hub is not None
        reported_lodging = planned_day.morning_lodging if is_departure_day else planned_day.lodging

        poi_by_id = {p.id: p for city_pois in poi_catalog.values() for p in city_pois}
        node_labels = {}
        for kind, ident, *_rest in chain:
            if kind == "poi":
                poi = poi_by_id.get(ident)
                node_labels[(kind, ident)] = (poi.name_en or poi.name) if poi else ident
            elif kind == "hub":
                hub = next(
                    (h for h in (plan.arrival_hub, plan.departure_hub) if h is not None and h.id == ident),
                    None,
                )
                node_labels[(kind, ident)] = (hub.name_en or hub.name) if hub else ident
            else:
                hotel = planned_day.morning_lodging if ident == planned_day.morning_lodging.id else planned_day.lodging
                node_labels[(kind, ident)] = hotel.name if hotel.id == ident else ident

        schedule = build_day_schedule(
            chain=chain,
            legs=legs,
            poi_by_id=poi_by_id,
            is_first_day=planned_day.day == 1,
            has_arrival_hub=plan.arrival_hub is not None,
            node_labels=node_labels,
            # T-034: arrival-day clock starts after landing; the departure
            # day's sightseeing must end before the flight cutoff.
            day_start_min=arrival_start_min if planned_day.day == 1 else None,
            day_end_cutoff_min=departure_cutoff_min if planned_day.day == req.days else None,
        )

        routes = [leg.route for leg in legs]
        # T-040: only POIs that actually got a visit row cost their ticket —
        # a spot dropped by opening-hours clipping (schedule visit skipped)
        # must not charge the traveller (kinkaku-ji case). Lunch-split visits
        # are still one ticket.
        visit_titles = {e["title"] for e in schedule if e["kind"] == "visit"}
        visited_pois = [
            p for p in planned_day.pois
            if (p.name_en or p.name) in visit_titles
        ]
        for p in planned_day.pois:
            if (p.name_en or p.name) not in visit_titles and not any(
                p.id in w for w in plan.warnings
            ):
                # Two reasons a spot can go unvisited (already closed / the day
                # is packed to its 22:00 cap); say which and give an action —
                # "closed" alone would be a lie (even 24h harajuku gets squeezed
                # out by an over-packed day, hit 2026-08-28).
                unvisited_warnings.append(
                    f"{p.id} is on the route but the day runs out before it — "
                    "try moving it to another day on the map"
                )
        # T-053: retract false-positive closing warnings — once schedule.py
        # replays with real transit times, any spot that actually got a visit
        # row invalidates its planner/terminal-check warning (a "spot X can't
        # be reached" warning next to a visited X is self-contradictory).
        visited_ids = {p.id for p in planned_day.pois if (p.name_en or p.name) in visit_titles}
        plan.warnings[:] = [
            w for w in plan.warnings
            if not any(uid in w for uid in visited_ids)
        ]
        itinerary.append({
            "day": planned_day.day,
            "city": planned_day.city,
            "lodging": reported_lodging,
            "nodes": [Node(kind=k, id=i, role=r) for (k, i, _, _, r, _) in chain],
            "poi_ids": [p.id for p in planned_day.pois],
            "legs": legs,
            "schedule": schedule,
            "transit_minutes": sum(r.total_duration_min for r in routes),
            "transit_cost": sum(r.total_cost for r in routes),
            "ticket_cost": round(sum(to_usd(p.ticket_price, p.ticket_currency) or 0 for p in visited_pois), 2),
            "currency": "USD",
            "transfer_count": sum(r.transfer_count for r in routes),
            "all_real_data": all(not r.estimated for r in routes),
        })

    # T-040: trip budget. Transit comes from the itinerary; tickets from the
    # day plans; stays count every night actually slept — the departure day
    # has no night when the trip ends at an airport/station. Unknown prices
    # count as 0 and are reported so the number stays honest.
    lodging_nights = [
        planned_day for planned_day in plan.days
        if not (planned_day.day == req.days and plan.departure_hub is not None)
    ]
    lodging_cost = round(sum(
        to_usd(pd.lodging.price_per_night, pd.lodging.price_currency) or 0
        for pd in lodging_nights
    ), 2)
    ticket_total = round(sum(day["ticket_cost"] for day in itinerary), 2)
    transit_total = round(sum(day["transit_cost"] for day in itinerary), 2)
    unknown_prices = (
        sum(1 for pd in plan.days for p in pd.pois if to_usd(p.ticket_price, p.ticket_currency) is None)
        + sum(1 for pd in lodging_nights if to_usd(pd.lodging.price_per_night, pd.lodging.price_currency) is None)
    )

    return {
        "cities": cities,
        "days": req.days,
        "hotel_mode": hotel_mode,
        "first_day_density": first_day_density,
        "last_day_density": last_day_density,
        "arrival_hub": plan.arrival_hub,
        "departure_hub": plan.departure_hub,
        # T-034: post-landing sightseeing start / pre-takeoff cutoff, in
        # minutes from midnight (hub-local). Null when no flight time given —
        # the frontend's schedule mirror uses these instead of hardcoding 09:00.
        "arrival_start_min": arrival_start_min,
        "departure_cutoff_min": departure_cutoff_min,
        # Honest per-request report (T-011): "deepseek" only if every city's
        # day-filling actually came from the model this call, "rule-based"
        # only if none did (including whenever no key is configured), else
        # "mixed" — never hardcoded, never claims deepseek without a real call.
        "grouper": plan.grouper,
        "transit_provider": transit.name,
        "warnings": plan.warnings + unvisited_warnings,
        "unselected_poi_ids": plan.unselected_poi_ids,
        "itinerary": itinerary,
        "totals": {
            "transit_minutes": sum(day["transit_minutes"] for day in itinerary),
            "transit_cost": transit_total,
            "ticket_cost": ticket_total,
            "lodging_cost": lodging_cost,
            "grand_total": round(transit_total + ticket_total + lodging_cost, 2),
            "currency": "USD",
            "transfer_count": sum(day["transfer_count"] for day in itinerary),
            "all_real_data": all(day["all_real_data"] for day in itinerary),
            "unknown_prices": unknown_prices,
        },
    }


@app.post("/api/trip/transit-legs")
def transit_legs(req: TransitLegsRequest) -> dict[str, object]:
    """Local re-query for frontend fine-tuning (T-013): same per-leg transit
    logic as ``/optimize-route`` (walking short-circuit, time-of-day, cache)
    but never touches DeepSeek/planner, and isn't rate-limited — callers are
    expected to hit this repeatedly (dragging a pin, swapping a stop) and
    should land on the cache almost every time.
    """
    if not req.pairs:
        raise HTTPException(status_code=400, detail="pairs must not be empty")
    if len(req.pairs) > MAX_TRANSIT_LEGS_PAIRS:
        raise HTTPException(status_code=400, detail="too many pairs")

    for pair in req.pairs:
        for node in (pair.from_, pair.to):
            if node.kind not in VALID_NODE_KINDS:
                raise HTTPException(status_code=400, detail=f"unknown kind: {node.kind}")
            if node.lat is None or node.lng is None:
                raise HTTPException(status_code=400, detail="lat and lng are required")

    transit = get_transit_provider()

    def _fetch(pair: LegPairRequest) -> Leg:
        origin = Coord(lat=pair.from_.lat, lng=pair.from_.lng)
        dest = Coord(lat=pair.to.lat, lng=pair.to.lng)
        tz_name = _timezone_for_lng(pair.from_.lng)
        route = _query_leg(transit, origin, dest, tz_name=tz_name, intercity=pair.intercity)
        return Leg(
            from_id=pair.from_.id, from_kind=pair.from_.kind,
            to_id=pair.to.id, to_kind=pair.to.kind,
            intercity=pair.intercity,
            route=route,
        )

    # T-046: the fine-tuning endpoint is parallel too (removing/adding a stop
    # on the frontend can introduce several new pairs at once).
    with ThreadPoolExecutor(max_workers=8) as pool:
        legs = list(pool.map(_fetch, req.pairs))
    return {"legs": legs}


# ---------------------------------------------------------------------------
# Agent mode (T-A1): Strands Agents SDK chat — conversational planning with
# human-in-the-loop interrupts. The agent orchestrates (chooses tools, asks
# questions); the native engine decides geography/feasibility. Zero live
# SerpApi calls by construction (cache + calibrated estimates only).
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import json as _json  # noqa: E402
import os as _os  # noqa: E402

from fastapi.responses import StreamingResponse  # noqa: E402

from .agents.factory import build_agent  # noqa: E402
from .agents.models import TripPlan as _TripPlan  # noqa: E402
from .agents.provenance import enrich as _enrich  # noqa: E402
from .agents.authority import rebuild_from_engine as _rebuild_from_engine  # noqa: E402

_agent_sessions: dict[str, object] = {}
# last draft_day_plan ARGS per session, captured from the SSE tool events —
# the final plan re-runs the engine with these args directly in this module
# (T-A7), so the displayed itinerary is engine truth regardless of what the
# model echoed.
_session_drafts: dict[str, dict] = {}


def _hhmm_arg(v) -> int | None:
    """Agent tool arg "HH:MM" -> minutes, or None."""
    try:
        h, m = str(v).strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _rerun_engine_draft(city: str, args: dict):
    """Deterministically re-run plan_trip with the agent's last draft args
    (rule path — no LLM). Returns the engine TripPlan object or None."""
    import json as _json

    from .services.factory import get_lodging_provider as _gl, get_poi_provider as _gp, \
        get_transport_hub_provider as _gh

    if isinstance(args, str):
        try:
            args = _json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    poi_ids = args.get("poi_ids") or []
    if not poi_ids:
        return None
    arrival_time_min = _hhmm_arg(args.get("arrival_time")) if args.get("arrival_time") else None
    departure_time_min = _hhmm_arg(args.get("departure_time")) if args.get("departure_time") else None
    try:
        return plan_trip(
            cities=[city],
            days=int(args.get("days", 2)),
            poi_ids=poi_ids,
            hotel_mode="system_one",
            custom_stays=[],
            poi_catalog={city: _gp().get_pois(city)},
            lodging_catalog={city: _gl().get_lodgings(city)},
            deepseek_api_key="",  # engine rule path — deterministic, no LLM
            deepseek_base_url="",
            arrival_hub_id=args.get("arrival_hub_id"),
            departure_hub_id=args.get("departure_hub_id"),
            hub_catalog={city: _gh().get_hubs(city)},
            arrival_time_min=arrival_time_min,
            departure_time_min=departure_time_min,
            first_day_density=args.get("first_day_density"),
            last_day_density=args.get("last_day_density"),
        )
    except Exception as exc:  # noqa: BLE001 — degrade to model plan on any error
        logger.warning("engine draft re-run failed: %s", exc)
        return None


class _ChainLeg:
    """build_day_schedule reads .route off each leg."""

    def __init__(self, route):
        self.route = route


def _render_days_classic(city: str, args: dict) -> dict | None:
    """Render the final itinerary through the EXACT classic pipeline:
    _build_day_chain -> per-leg _query_leg -> build_day_schedule.

    One implementation for both modes. Every classic behaviour is inherited
    here instead of re-approximated: arrival hub opens the chain with a
    check-in row, the departure hub closes the last day (no hotel return,
    no dinner after), flight buffers shape the day clock, closing-time
    clipping and meals come from the same schedule function classic uses.
    """
    plan = _rerun_engine_draft(city, args)
    if plan is None:
        return None
    if isinstance(args, str):
        import json as _json

        try:
            args = _json.loads(args)
        except ValueError:
            args = {}
    args = args if isinstance(args, dict) else {}

    n_days = len(plan.days)
    poi_by_id = {p.id: p for p in get_poi_provider().get_pois(city)}
    transit = get_transit_provider()

    arrival_time_min = _hhmm_arg(args.get("arrival_time")) if args.get("arrival_time") else None
    departure_time_min = _hhmm_arg(args.get("departure_time")) if args.get("departure_time") else None
    arrival_start_min = arrival_time_min + LANDING_BUFFER_MIN if arrival_time_min is not None else None
    departure_cutoff_min = departure_time_min - TAKEOFF_BUFFER_MIN if departure_time_min is not None else None

    warnings: list[str] = list(plan.warnings)
    days_out = []
    for planned_day in plan.days:
        chain = _build_day_chain(planned_day, n_days, plan.arrival_hub, plan.departure_hub)
        node_labels: dict[tuple[str, str], str] = {}
        for kind, ident, *_rest in chain:
            if kind == "poi":
                poi = poi_by_id.get(ident)
                node_labels[(kind, ident)] = (poi.name_en or poi.name) if poi else ident
            elif kind == "hub":
                hub = next(
                    (h for h in (plan.arrival_hub, plan.departure_hub) if h is not None and h.id == ident),
                    None,
                )
                node_labels[(kind, ident)] = (hub.name_en or hub.name) if hub else ident
            else:
                hotel = planned_day.morning_lodging if ident == planned_day.morning_lodging.id else planned_day.lodging
                node_labels[(kind, ident)] = hotel.name if hotel.id == ident else ident

        hour_override = arrival_start_min // 60 if (planned_day.day == 1 and arrival_start_min is not None) else None
        legs = []
        for (from_kind, from_id, from_lat, from_lng, _fr, from_city), \
                (to_kind, to_id, to_lat, to_lng, _tr, to_city) in zip(chain, chain[1:]):
            route = _query_leg(
                transit, Coord(lat=from_lat, lng=from_lng), Coord(lat=to_lat, lng=to_lng),
                tz_name=CITY_TIMEZONES.get(from_city, "Asia/Tokyo"),
                intercity=from_city != to_city,
                trip_day=planned_day.day, city=from_city,
                hour_override=hour_override,
            )
            legs.append(_ChainLeg(route))

        schedule = build_day_schedule(
            chain=chain, legs=legs, poi_by_id=poi_by_id,
            is_first_day=planned_day.day == 1,
            has_arrival_hub=plan.arrival_hub is not None,
            node_labels=node_labels,
            day_start_min=arrival_start_min if planned_day.day == 1 else None,
            day_end_cutoff_min=departure_cutoff_min if planned_day.day == n_days else None,
        )

        visits = {e["title"]: (e["start"], e["end"]) for e in schedule if e["kind"] == "visit"}
        # spots that never got a visit row (closed by arrival) must not linger
        # as ghost transit anchors — their legs are dropped; the warning names
        # them instead
        unvisited_titles = {
            (poi.name_en or poi.name) for poi in planned_day.pois
            if (poi.name_en or poi.name) not in visits
        }
        chain_rows = []
        for e in schedule:
            if e["kind"] not in ("transit", "visit", "lunch", "dinner", "checkin"):
                continue
            if e["kind"] == "transit" and " → " in e["title"]:
                src, dst = e["title"].split(" → ", 1)
                if src in unvisited_titles or dst in unvisited_titles:
                    continue  # ghost leg touching a never-visited spot
            row = {"kind": e["kind"], "start": e["start"], "end": e["end"], "title": e["title"]}
            if e.get("line_summary"):
                row["summary"] = e["line_summary"]
            chain_rows.append(row)

        spots = []
        for poi in planned_day.pois:
            title = poi.name_en or poi.name
            if title in visits:
                start, end = visits[title]
                spots.append({"id": poi.id, "name_en": title, "start": start, "end": end, "note": ""})
            elif not any(poi.id in w for w in warnings):
                warnings.append(
                    f"{poi.id} is on the route but couldn't be visited (closed by the time "
                    "you'd arrive) — say the word and I'll re-plan"
                )

        is_departure_day = planned_day.day == n_days and plan.departure_hub is not None
        reported_hotel = planned_day.morning_lodging if is_departure_day else planned_day.lodging
        start_val = arrival_start_min if planned_day.day == 1 and arrival_start_min is not None else 9 * 60
        days_out.append({
            "day": planned_day.day,
            "city": planned_day.city,
            "day_start": f"{start_val // 60:02d}:{start_val % 60:02d}",
            "chain": chain_rows,
            "spots": spots,
            "hotel": reported_hotel.name,
            "hotel_lat": reported_hotel.lat,
            "hotel_lng": reported_hotel.lng,
            "summary": "",
        })

    return {"days": days_out, "warnings": warnings}
_agent_lock = asyncio.Lock()  # one agent loop at a time (demo scale)


def _agent_sse(kind: str, **payload) -> str:
    return f"data: {_json.dumps({'type': kind, **payload}, ensure_ascii=False)}\n\n"


def _agent_for(session_id: str):
    if session_id not in _agent_sessions:
        _agent_sessions[session_id] = build_agent()
    return _agent_sessions[session_id]


class AgentChatBody(BaseModel):
    message: str
    session_id: str = "web"


class AgentResumeBody(BaseModel):
    session_id: str = "demo"
    interrupt_id: str
    response: str


class AgentResetBody(BaseModel):
    session_id: str = "demo"


@app.get("/api/trip/agent/spots/{city}")
def agent_spots(city: str):
    """Catalog coordinates so the agent-mode map can plot the plan without
    the model having to echo lat/lng in structured output."""
    return [
        {"id": p.id, "name_en": p.name_en or p.name, "lat": p.lat, "lng": p.lng}
        for p in get_poi_provider().get_pois(city)
    ]


@app.get("/api/trip/agent/hotels/{city}")
def agent_hotels(city: str):
    """Lodging catalog coordinates so the agent-mode map can plot the hotels
    the plan references by name."""
    return [
        {"name": l.name, "lat": l.lat, "lng": l.lng}
        for l in get_lodging_provider().get_lodgings(city)
    ]


@app.get("/api/trip/agent/info")
def agent_info():
    """Build metadata for the terminal statusline (model / version / tools)."""
    model = (
        "bedrock:" + _os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
        if _os.environ.get("AGENT_MODEL") == "bedrock"
        else "deepseek-chat"
    )
    return {
        "app": "TripAgent",
        "version": "1.0.0",
        "model": model,
        "framework": "strands-agents",
        "tools": 8,
    }


@app.post("/api/trip/agent/reset")
def agent_reset(body: AgentResetBody):
    """Drop a wedged/broken session's agent (interrupt state is not resumable
    with a fresh string prompt)."""
    _agent_sessions.pop(body.session_id, None)
    return {"status": "reset", "session_id": body.session_id}


def _agent_stream(session_id: str, payload):
    agent = _agent_for(session_id)
    MAX_TOOL_EVENTS = 150  # T-A4 hard stop: a looping agent cannot burn the
    # traveller's time — the structured follow-up still produces a plan.

    async def generate():
        final = None
        capped = False
        tool_calls_seen: set[str] = set()  # unique toolUseIds — strands emits
        # many current_tool_use events per call while streaming its input
        try:
            async with _agent_lock:
                async for event in agent.stream_async(payload):
                    if "data" in event:
                        yield _agent_sse("text", delta=event["data"])
                    elif event.get("current_tool_use", {}).get("name"):
                        tool = event["current_tool_use"]
                        call_id = str(tool.get("toolUseId") or tool.get("id") or "")
                        # capture on EVERY event: strands streams the input
                        # incrementally, so the first event of a call carries
                        # an EMPTY/partial input — the last write wins with
                        # the complete args
                        if tool["name"] == "draft_day_plan":
                            _session_drafts[session_id] = tool.get("input", {}) or {}
                        if call_id and call_id in tool_calls_seen:
                            continue  # same call streaming — count once
                        if call_id:
                            tool_calls_seen.add(call_id)
                        if len(tool_calls_seen) > MAX_TOOL_EVENTS:
                            capped = True  # stop the loop; salvage below
                            break
                        yield _agent_sse("tool", name=tool["name"],
                                         input_preview=str(tool.get("input", {}))[:160])
                    elif "result" in event:
                        final = event["result"]
        except Exception as exc:  # a wedged stream must fail visibly
            yield _agent_sse("error", message=f"{type(exc).__name__}: {exc}")
            yield _agent_sse("done", state="error")
            return
        if capped:
            def _emit_plan():
                return agent(
                    "You hit the tool-call budget. Emit the best itinerary you have "
                    "as the structured TripPlan right now - no new tools.",
                    structured_output_model=_TripPlan,
                )
            try:
                final = await asyncio.to_thread(_emit_plan)
            except Exception:
                await asyncio.sleep(3)
                final = await asyncio.to_thread(_emit_plan)
        if final is not None and getattr(final, "stop_reason", None) != "interrupt" \
                and getattr(final, "structured_output", None) is None:
            # stream_async can't take structured_output_model per-call — a
            # prose-ending turn gets one forced structured follow-up so the
            # plan card always renders.
            def _emit_plan():
                return agent(
                    "Emit the final confirmed itinerary as the structured TripPlan now — "
                    "no new tools, just the structured answer.",
                    structured_output_model=_TripPlan,
                )

            try:
                final = await asyncio.to_thread(_emit_plan)
            except Exception as exc:
                # a previous stream on this session may still be finishing —
                # wait once and retry before giving up
                await asyncio.sleep(3)
                try:
                    final = await asyncio.to_thread(_emit_plan)
                except Exception as exc2:
                    yield _agent_sse("error", message=f"plan emit failed: {type(exc2).__name__}: {exc2}")
                    yield _agent_sse("done", state="error")
                    return
        if final is None:
            yield _agent_sse("error", message="agent produced no result")
            return
        if getattr(final, "stop_reason", None) == "interrupt":
            for intr in final.interrupts or []:
                reason = intr.reason if isinstance(intr.reason, dict) else {}
                yield _agent_sse(
                    "decision", interrupt_id=intr.id, question=reason.get("question", ""),
                    options=reason.get("options", []), context=reason.get("context", ""),
                )
            yield _agent_sse("done", state="waiting_for_decision")
            return
        plan = getattr(final, "structured_output", None)
        if plan is not None:
            plan_dict = plan.model_dump()  # not `payload` — closure param shadowing
            # T-A7: re-run the engine with the captured draft args and REBUILD
            # the itinerary from planner.py truth (classic-mode source);
            # the model's echo only contributes display notes.
            _draft_args = _session_drafts.get(session_id) or {}
            _city = plan_dict["days"][0].get("city") if plan_dict.get("days") else _draft_args.get("city", "")
            logger.info("plan emit: session=%s captured_args=%s city=%r",
                        session_id, list(_draft_args.keys())[:8] if isinstance(_draft_args, dict) else type(_draft_args).__name__, _city)
            # reconcile flight times: the model states them in the final
            # plan even when a tool call missed them — the renderer's cutoff
            # must never depend on the model remembering one specific call.
            # (args may arrive as a JSON string — parse before merging)
            import json as _json7

            if isinstance(_draft_args, str):
                try:
                    _draft_args = _json7.loads(_draft_args)
                except ValueError:
                    _draft_args = {}
            if isinstance(_draft_args, dict):
                for _k in ("arrival_time", "departure_time"):
                    if not _draft_args.get(_k) and plan_dict.get(_k):
                        _draft_args[_k] = plan_dict[_k]
            _rendered = _render_days_classic(_city, _draft_args) if (_draft_args and _city) else None
            logger.info("plan emit: classic renderer -> %s", "OK" if _rendered is not None else "FALLBACK")
            if _rendered is not None:
                plan_dict["days"] = _rendered["days"]
                plan_dict["warnings"] = _rendered["warnings"] + [
                    w for w in plan_dict.get("warnings", []) if w not in _rendered["warnings"]
                ][:2]
            else:
                plan_dict = _rebuild_from_engine(plan_dict)
            extra = _enrich(plan_dict["days"][0]["city"], plan_dict) if plan.days else {}
            yield _agent_sse("plan", plan=plan_dict, **extra)
        yield _agent_sse("done", state="complete")

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/trip/agent/chat")
async def agent_chat(body: AgentChatBody):
    return _agent_stream(body.session_id, body.message)


@app.post("/api/trip/agent/resume")
async def agent_resume(body: AgentResumeBody):
    if body.session_id not in _agent_sessions:
        return {"error": "unknown session"}
    resume_payload = [{
        "interruptResponse": {"interruptId": body.interrupt_id, "response": body.response},
    }]
    return _agent_stream(body.session_id, resume_payload)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    # Loopback only: production reverse-proxies to 127.0.0.1 via Nginx, so the
    # port is never exposed to the public internet
    uvicorn.run(app, host="127.0.0.1", port=settings.app_port)
