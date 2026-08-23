"""FastAPI entry point.

Run locally (from the backend/ directory):

    uvicorn app.main:app --reload --port 5003   # dev
    python -m app.main                          # uses APP_PORT (default 5003)

Works with zero API keys (D-010): without SERPAPI_KEY / DEEPSEEK_API_KEY the
local JSON and rule-based implementations are selected automatically.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import get_cors_origins, get_settings
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
from .services.schedule import build_day_schedule
from .services.search import search_lodgings, search_pois
from .services.transit import Coord, TransitRoute, estimate_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# T-016: ``country`` groups the city picker on the frontend (Japan/China/Korea).
CITIES = [
    {"id": "tokyo", "name": "东京", "name_en": "Tokyo", "country": "japan"},
    {"id": "osaka", "name": "大阪", "name_en": "Osaka", "country": "japan"},
    {"id": "kyoto", "name": "京都", "name_en": "Kyoto", "country": "japan"},
    {"id": "beijing", "name": "北京", "name_en": "Beijing", "country": "china"},
    {"id": "shanghai", "name": "上海", "name_en": "Shanghai", "country": "china"},
    {"id": "hongkong", "name": "香港", "name_en": "Hong Kong", "country": "china"},
    {"id": "seoul", "name": "首尔", "name_en": "Seoul", "country": "korea"},
    {"id": "busan", "name": "釜山", "name_en": "Busan", "country": "korea"},
    {"id": "jeju", "name": "济州", "name_en": "Jeju", "country": "korea"},
]
CITY_IDS = {city["id"] for city in CITIES}
COUNTRY_NAMES = {"japan": "Japan", "china": "China", "korea": "Korea"}

# T-012 / 方案 7.3: query transit at a plausible local time, not server time.
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
    hardcoding Shanghai server time (方案 7.3) without needing a city field.
    """
    return "Asia/Shanghai" if lng < 124 else "Asia/Tokyo"


def _query_leg(
    transit, origin: Coord, dest: Coord, *, tz_name: str, intercity: bool, trip_day: int = 1, city: str = "",
) -> TransitRoute:
    """Shared by ``/optimize-route`` and ``/transit-legs`` (T-013): same
    time-of-day rule, same cache, same per-leg estimate fallback so a single
    bad SerpApi call never fails the whole request."""
    hour = INTERCITY_HOUR if intercity else INTRA_CITY_HOUR
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
    allow_origins=get_cors_origins(),  # CORS_ORIGINS 环境变量可配，默认 dev + preview
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CustomStayBody(BaseModel):
    lodging_id: str
    days: list[int]


class OptimizeRequest(BaseModel):
    """New contract (实现批次.md 2.6). ``city`` (singular) is the pre-T-010
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


@app.get("/api/trip/health")
def health() -> dict[str, str]:
    """Liveness probe; also reports which data sources are active."""
    return {
        "status": "ok",
        "poi_provider": get_poi_provider().name,
        "lodging_provider": get_lodging_provider().name,
        "transit_provider": get_transit_provider().name,
        "grouper": get_grouper().name,
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
        )
        first_day_density, last_day_density = resolve_edge_modes(first_d, last_d, req.edge_density)
    except PlanningError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    transit = get_transit_provider()
    itinerary: list[dict[str, object]] = []
    for planned_day in plan.days:
        chain = _build_day_chain(planned_day, req.days, plan.arrival_hub, plan.departure_hub)
        legs: list[Leg] = []
        for (from_kind, from_id, from_lat, from_lng, _from_role, from_city), \
            (to_kind, to_id, to_lat, to_lng, _to_role, to_city) in zip(chain, chain[1:]):
            intercity = from_city != to_city
            origin, dest = Coord(lat=from_lat, lng=from_lng), Coord(lat=to_lat, lng=to_lng)
            tz_name = CITY_TIMEZONES.get(from_city, "Asia/Tokyo")
            route = _query_leg(
                transit, origin, dest, tz_name=tz_name, intercity=intercity,
                trip_day=planned_day.day, city=from_city,
            )
            legs.append(Leg(
                from_id=from_id, from_kind=from_kind,
                to_id=to_id, to_kind=to_kind,
                intercity=intercity,
                route=route,
            ))

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
        )

        routes = [leg.route for leg in legs]
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
            "currency": "USD",
            "transfer_count": sum(r.transfer_count for r in routes),
            "all_real_data": all(not r.estimated for r in routes),
        })

    return {
        "cities": cities,
        "days": req.days,
        "hotel_mode": hotel_mode,
        "first_day_density": first_day_density,
        "last_day_density": last_day_density,
        "arrival_hub": plan.arrival_hub,
        "departure_hub": plan.departure_hub,
        # Honest per-request report (T-011): "deepseek" only if every city's
        # day-filling actually came from the model this call, "rule-based"
        # only if none did (including whenever no key is configured), else
        # "mixed" — never hardcoded, never claims deepseek without a real call.
        "grouper": plan.grouper,
        "transit_provider": transit.name,
        "warnings": plan.warnings,
        "unselected_poi_ids": plan.unselected_poi_ids,
        "itinerary": itinerary,
        "totals": {
            "transit_minutes": sum(day["transit_minutes"] for day in itinerary),
            "transit_cost": sum(day["transit_cost"] for day in itinerary),
            "currency": "USD",
            "transfer_count": sum(day["transfer_count"] for day in itinerary),
            "all_real_data": all(day["all_real_data"] for day in itinerary),
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
    legs: list[Leg] = []
    for pair in req.pairs:
        origin = Coord(lat=pair.from_.lat, lng=pair.from_.lng)
        dest = Coord(lat=pair.to.lat, lng=pair.to.lng)
        tz_name = _timezone_for_lng(pair.from_.lng)
        route = _query_leg(transit, origin, dest, tz_name=tz_name, intercity=pair.intercity)
        legs.append(Leg(
            from_id=pair.from_.id, from_kind=pair.from_.kind,
            to_id=pair.to.id, to_kind=pair.to.kind,
            intercity=pair.intercity,
            route=route,
        ))
    return {"legs": legs}


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    # 只监听本机回环：生产由 Nginx 反代到 127.0.0.1，端口不暴露公网
    uvicorn.run(app, host="127.0.0.1", port=settings.app_port)
