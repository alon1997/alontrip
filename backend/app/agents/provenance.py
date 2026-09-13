"""Provenance + per-day arithmetic for the agent-mode plan card.

Computed SERVER-SIDE from the catalog and the transit provider — never from
the model's own claims. The model never writes the score; it only gets to
say which tool it used.
"""

from __future__ import annotations

from ..services.currency import to_usd
from ..services.transit import Coord

from .tools import lodging_provider, poi_provider, transit_provider


def _leg_profile(city: str, a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> dict:
    route = transit_provider().get_route(
        Coord(lat=a_lat, lng=a_lng), Coord(lat=b_lat, lng=b_lng), city=city,
    )
    walk_only = bool(route.legs) and all(leg.travel_mode == "walking" for leg in route.legs)
    if walk_only:
        source = "walk"
    else:
        source = route.data_source or ("estimate" if route.estimated else "cache")
    return {"source": source, "minutes": route.total_duration_min, "usd": route.total_cost}


def enrich(city: str, plan: dict) -> dict:
    """Attach provenance + per-day costs to a structured TripPlan dict.

    Returns {"provenance": {...}, "day_costs": [...]}. Unknown spot ids are
    counted as invented (they should be 0 — draft_day_plan rejects them, so
    a non-zero here means the model drifted and the UI shows a red flag).
    """
    catalog = {p.id: p for p in poi_provider().get_pois(city)}
    spot_ids = [s["id"] for d in plan.get("days", []) for s in d.get("spots", [])]
    verified = [sid for sid in spot_ids if sid in catalog]
    invented = [sid for sid in spot_ids if sid not in catalog]

    transit = {"cache": 0, "estimate": 0, "walk": 0}
    day_costs = []
    for d in plan.get("days", []):
        spots = [catalog[s["id"]] for s in d.get("spots", []) if s["id"] in catalog]
        t_usd = 0.0
        for a, b in zip(spots, spots[1:]):
            prof = _leg_profile(city, a.lat, a.lng, b.lat, b.lng)
            transit[prof["source"]] = transit.get(prof["source"], 0) + 1
            t_usd += prof["usd"]
        t_usd = round(t_usd, 2)
        tickets = 0.0
        unknown = 0
        for p in spots:
            tk = to_usd(p.ticket_price, p.ticket_currency)
            if tk is None:
                unknown += 1
            else:
                tickets += tk
        day_costs.append({
            "day": d.get("day"),
            "transit_usd": t_usd,
            "tickets_usd": round(tickets, 2),
            "total_usd": round(t_usd + tickets, 2),
            "unknown_ticket_spots": unknown,
        })

    seen: set[str] = set()
    duplicates: list[str] = []
    for sid in spot_ids:
        if sid in seen and sid not in duplicates:
            duplicates.append(sid)
        seen.add(sid)

    provenance = {
        "spots_total": len(spot_ids),
        "spots_catalog_verified": len(verified),
        "spots_invented": len(invented),
        "invented_ids": invented,
        "duplicate_spot_ids": duplicates,
        "transit_legs": transit,
        "live_api_calls": 0,  # no SerpApi key on this deployment — structural
    }
    return {"provenance": provenance, "day_costs": day_costs}
