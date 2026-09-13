"""Agent tools (agent mode) — thin, docstring-driven wrappers around the
native services (factory providers + planner/schedule/hours).

Contract mirrored from the hackathon repo's agent/tools.py: the docstring IS
the tool description the model sees; type hints ARE the input schema. Heavy
lifting (geographic grouping, capacity packing, closing-time terminal check)
stays in services/planner.py — the model orchestrates, the engine decides.
"""

from __future__ import annotations

import os

from strands import tool

from ..services.factory import (
    get_lodging_provider,
    get_poi_provider,
    get_transport_hub_provider,
)
from ..services.hours import parse_hours
from ..services.planner import PlanningError, plan_trip
from ..services.schedule import build_day_schedule
from ..services.transit import Coord, LocalTransitProvider, SerpApiTransitProvider


class _Leg:
    """build_day_schedule reads .route off each leg."""

    def __init__(self, route):
        self.route = route


def _providers():
    """Lazy provider singletons (resolved per call — cheap JSON/DB readers)."""
    from ..services.factory import (  # local import keeps import graph flat
        get_lodging_provider as _lod,
        get_poi_provider as _poi,
        get_transport_hub_provider as _hub,
    )
    return _poi(), _lod(), _hub()


def poi_provider():
    return _providers()[0]


def lodging_provider():
    return _providers()[1]


def transit_provider():
    """Agent-scoped transit provider: live SerpApi (sharing the on-disk
    cache) when AGENT_SERPAPI_KEY is configured, else the keyless
    cache/estimate provider. The classic tool mode keeps using the global
    factory provider, so quota is only spent by agent conversations."""
    key = os.environ.get("AGENT_SERPAPI_KEY")
    if not key:
        from ..config import get_settings

        key = get_settings().agent_serpapi_key
    if key:
        from ..services.factory import TRANSIT_CACHE_DIR

        return SerpApiTransitProvider(api_key=key, cache_dir=TRANSIT_CACHE_DIR)
    return LocalTransitProvider(cache_dir=_transit_cache_dir())


def _transit_cache_dir():
    from pathlib import Path

    from ..services.factory import REPO_ROOT

    return Path(REPO_ROOT) / "data" / "transit_cache"


@tool
def list_cities() -> list[dict]:
    """List supported cities with their spot counts (East Asia budget-trip
    coverage: Japan, South Korea, Greater China)."""
    from ..main import CITIES  # call-time import — avoids main↔agents cycle

    poi = poi_provider()
    return [{"city": c["id"], "spots": len(poi.get_pois(c["id"]))} for c in CITIES]


@tool
def search_pois(city: str, query: str = "") -> list[dict]:
    """List tourist spots in a city, optionally filtered by a keyword.

    Args:
        city: one of the supported cities (see list_cities), e.g. "kyoto".
        query: optional keyword matched against name/id (case-insensitive).

    Each spot has: id, name_en, lat, lng, visit_minutes (suggested stay),
    ticket_usd, opening_hours, and the parsed opens/closes window. Research
    in THIS one call — opens/closes are already included, so you normally do
    NOT need spot_detail afterwards.
    """
    poi = poi_provider()
    spots = poi.get_pois(city)
    if query:
        q = query.lower()
        spots = [p for p in spots if q in p.id.lower() or q in (p.name_en or p.name).lower()]
    out = []
    for p in spots:
        opens, closes = parse_hours(p.opening_hours)
        hhmm = lambda m: None if m is None else f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
        out.append({
            "id": p.id,
            "name_en": p.name_en or p.name,
            "lat": p.lat,
            "lng": p.lng,
            "visit_minutes": p.suggested_duration_min,
            "ticket_usd": p.ticket_price,
            "opening_hours": p.opening_hours,
            "opens": hhmm(opens),
            "closes": hhmm(closes),
        })
    return out


@tool
def spot_detail(city: str, poi_id: str) -> dict:
    """Full record for one spot: suggested stay, ticket price, and the parsed
    opening window (opens/closes as HH:MM, null when unknown)."""
    p = next((x for x in poi_provider().get_pois(city) if x.id == poi_id), None)
    if p is None:
        return {"error": f"unknown poi_id {poi_id!r} for city {city!r}"}
    opens, closes = parse_hours(p.opening_hours)
    hhmm = lambda m: None if m is None else f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
    return {
        "id": p.id,
        "name_en": p.name_en or p.name,
        "lat": p.lat,
        "lng": p.lng,
        "visit_minutes": p.suggested_duration_min,
        "ticket_usd": p.ticket_price,
        "opening_hours_raw": p.opening_hours,
        "opens": hhmm(opens),
        "closes": hhmm(closes),
    }


def _transit_profile(city: str, a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> dict:
    route = transit_provider().get_route(
        Coord(lat=a_lat, lng=a_lng), Coord(lat=b_lat, lng=b_lng), city=city,
    )
    lines = [leg.line_name for leg in route.legs if leg.travel_mode == "transit" and leg.line_name]
    walk_only = bool(route.legs) and all(leg.travel_mode == "walking" for leg in route.legs)
    source = "walk" if walk_only else (route.data_source or ("estimate" if route.estimated else "cache"))
    return {
        "minutes": route.total_duration_min,
        "usd": round(route.total_cost, 2),
        "estimated": route.estimated,
        "source": source,
        "lines": lines[:4],
        "summary": " + ".join(lines) if lines else ("walk" if walk_only else "walk/taxi estimate"),
    }


@tool
def transit_route(
    from_lat: float, from_lng: float, to_lat: float, to_lng: float, city: str = ""
) -> dict:
    """Public-transit minutes and fare between two coordinates.

    Answers from a real cached Google-Maps query when one exists, otherwise
    from a distance-calibrated estimate (marked estimated=true). Use this
    instead of guessing travel times.
    """
    return _transit_profile(city, from_lat, from_lng, to_lat, to_lng)


@tool
def draft_day_plan(
    city: str,
    days: int,
    poi_ids: list[str],
    arrival_hub_id: str | None = None,
    arrival_time: str | None = None,
    first_day_density: str | None = None,
    last_day_density: str | None = None,
) -> dict:
    """Draft a day-by-day itinerary with the battle-tested geographic engine.

    Groups the given spots into `days` days (geographically contiguous),
    picks nightly hostels, and runs the closing-time feasibility check.
    Returns per-day spot lists plus any honest warnings.

    Args:
        city: city id, e.g. "kyoto".
        days: total days for this city (2-14).
        poi_ids: spot ids the traveller picked (each used exactly once).
        arrival_hub_id: optional airport/station id for day 1.
        arrival_time: optional "HH:MM" landing time (24h) with arrival_hub_id.
        first_day_density / last_day_density: "few" (light) or "none" (empty)
          for the arrival/departure day.
    """
    poi = poi_provider()
    by_id = {p.id: p for p in poi.get_pois(city)}
    unknown = [pid for pid in poi_ids if pid not in by_id]
    if unknown:
        return {"error": f"unknown poi_ids: {unknown}"}
    arrival_time_min = None
    if arrival_time:
        try:
            h, m = arrival_time.strip().split(":")
            arrival_time_min = int(h) * 60 + int(m)
        except ValueError:
            return {"error": "arrival_time must be HH:MM (24h)"}
    try:
        plan = plan_trip(
            cities=[city],
            days=days,
            poi_ids=poi_ids,
            hotel_mode="system_one",
            custom_stays=[],
            poi_catalog={city: poi.get_pois(city)},
            lodging_catalog={city: lodging_provider().get_lodgings(city)},
            arrival_hub_id=arrival_hub_id,
            hub_catalog={city: get_transport_hub_provider().get_hubs(city)},
            arrival_time_min=arrival_time_min,
            first_day_density=first_day_density,
            last_day_density=last_day_density,
        )
    except PlanningError as exc:  # frozen 400 strings — surface, don't swallow
        return {"error": str(exc)}
    return {
        "days": [
            {
                "day": d.day,
                "spots": [
                    {"id": p.id, "name_en": p.name_en or p.name, "visit_minutes": p.suggested_duration_min}
                    for p in d.pois
                ],
                "hotel": d.lodging.name,
            }
            for d in plan.days
        ],
        "warnings": plan.warnings,
    }


@tool
def replay_clock(city: str, day_plan: dict, day_start: str = "") -> dict:
    """Replay one day's plan against real transit times to produce a timed
    schedule (transit legs, check-in, lunch ~12:00, dinner 18:00, visits
    clipped at closing). Use the returned event times VERBATIM in the final
    itinerary. `day_plan` is one day as returned by draft_day_plan.

    Args:
        city: city id.
        day_plan: one day from draft_day_plan.
        day_start: optional clock start "HH:MM" for this day — REQUIRED on
          arrival days (landing time + ~90 min into the city) so visits are
          timed from real arrival, not 09:00.
    """
    day_start_min = 9 * 60
    if day_start:
        try:
            h, m = day_start.strip().split(":")
            day_start_min = int(h) * 60 + int(m)
        except ValueError:
            return {"error": "day_start must be HH:MM (24h)"}
    pois = {p.id: p for p in poi_provider().get_pois(city)}
    spots = day_plan.get("spots", [])
    def _sid(s):
        return s["id"] if isinstance(s, dict) else s
    missing = [s for s in map(_sid, spots) if s not in pois]
    if missing:
        return {"error": f"unknown spot ids: {missing}"}
    ordered = [pois[_sid(s)] for s in spots]
    if not ordered:
        return {"events": [], "note": "empty day"}
    events, engine_times = replay_day_events(city, ordered, day_start_min)
    return {"events": events, "engine_times": engine_times, "day_start": day_start or "09:00"}


def replay_day_events(city: str, ordered: list, day_start_min: int = 540):
    """Engine-authoritative clock replay for one ordered day (no LLM involved).
    Returns (events, times) where times maps spot id -> (start, end) — the
    only visit times the product is allowed to display."""
    lodging = lodging_provider().get_lodgings(city)[0]  # clock origin only
    labels = {("poi", p.id): p.name_en or p.name for p in ordered}
    chain = [("lodging", lodging.id, lodging.lat, lodging.lng, "start", city)] + [
        ("poi", p.id, p.lat, p.lng, None, city) for p in ordered
    ] + [("lodging", lodging.id, lodging.lat, lodging.lng, "end", city)]
    legs = [
        _Leg(transit_provider().get_route(
            Coord(lat=a.lat, lng=a.lng), Coord(lat=b.lat, lng=b.lng), city=city,
        ))
        for a, b in zip([lodging, *ordered], [*ordered, lodging])  # hotel→p1 … pn→hotel
    ]
    events = build_day_schedule(
        chain=chain, legs=legs, poi_by_id={p.id: p for p in ordered},
        is_first_day=False, has_arrival_hub=False, node_labels=labels,
        day_start_min=day_start_min,
    )
    times: dict[str, tuple[str, str]] = {}
    for ev in events:
        if ev.get("kind") != "visit":
            continue
        for p in ordered:
            if (p.name_en or p.name) == ev["title"]:
                start, end = times.get(p.id, (ev["start"], ev["end"]))
                times[p.id] = (min(start, ev["start"]), max(end, ev["end"]))
    return events, times


@tool
def trip_budget(city: str, day_plans: list[dict]) -> dict:
    """Estimate trip cost (USD): transit, spot tickets for spots that can
    actually be visited, and nightly stays. `day_plans` = draft_day_plan's
    `days` list."""
    from ..services.currency import to_usd

    pois = {p.id: p for p in poi_provider().get_pois(city)}
    lodgings = {l.name: l for l in lodging_provider().get_lodgings(city)}
    transit_usd = tickets_usd = stays_usd = 0.0
    unknown = 0
    for i, dp in enumerate(day_plans):
        stops = [pois[s["id"] if isinstance(s, dict) else s] for s in dp.get("spots", [])]
        for a, b in zip(stops, stops[1:]):
            transit_usd += transit_provider().get_route(
                Coord(lat=a.lat, lng=a.lng), Coord(lat=b.lat, lng=b.lng), city=city,
            ).total_cost
        lodging = lodgings.get(dp.get("hotel", ""))
        if lodging is not None and i < len(day_plans) - 1:
            stays_usd += to_usd(lodging.price_per_night, lodging.price_currency) or 0
        for p in stops:
            t = to_usd(p.ticket_price, p.ticket_currency)
            if t is None:
                unknown += 1
            else:
                tickets_usd += t
    return {
        "transit_usd": round(transit_usd, 2),
        "tickets_usd": round(tickets_usd, 2),
        "stays_usd": round(stays_usd, 2),
        "total_usd": round(transit_usd + tickets_usd + stays_usd, 2),
        "unknown_prices": unknown,
    }
