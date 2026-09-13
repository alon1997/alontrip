"""Engine-authoritative times for the structured plan.

Whatever the model writes into TripPlan start/end fields is a DRAFT. Before
the plan reaches the traveller's screen, every day is replayed through the
deterministic engine (real transit legs, closing-time clipping, day start)
and the display times are OVERWRITTEN with the engine's. This is what makes
"the model never invents a place, a fare or a closing time" literally true —
a 13:00–22:00 Disney visit cannot survive this function when the park closes
at 21:00.
"""

from __future__ import annotations

from .tools import lodging_provider, poi_provider, replay_day_events


def _hhmm_to_min(v: str) -> int:
    try:
        h, m = v.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 9 * 60


def apply_engine_times(plan: dict) -> tuple[dict, list[str]]:
    """Rewrite every day's spot times with engine replay output.

    Returns (plan, notes). Spots the engine could not visit keep their draft
    times and get an honest note appended — removal would hide information,
    but the note makes the limitation visible.
    """
    if not plan.get("days"):
        return plan, []
    city = plan["days"][0].get("city", "")
    catalog = {p.id: p for p in poi_provider().get_pois(city)}
    notes: list[str] = []

    # attach hotel coordinates (exact-then-substring match against the
    # catalog) so the frontend never has to guess by display name. When the
    # model invented a free-form hotel name, fall back to the engine rule:
    # the catalog lodging nearest that day's spot centroid — and replace the
    # display name with the real one (the model's "to be confirmed" noise
    # would otherwise promise a hotel that was never selected).
    from math import sqrt

    lodgings = lodging_provider().get_lodgings(city)
    catalog = {p.id: p for p in poi_provider().get_pois(city)}
    for day in plan["days"]:
        name = day.get("hotel", "")
        match = next((l for l in lodgings if l.name == name), None) \
            or next((l for l in lodgings if name and (name in l.name or l.name in name)), None)
        if match is None and lodgings:
            coords = [catalog[s["id"]] for s in day.get("spots", []) if s["id"] in catalog]
            if coords:
                clat = sum(c.lat for c in coords) / len(coords)
                clng = sum(c.lng for c in coords) / len(coords)
                match = min(
                    lodgings,
                    key=lambda l: sqrt((l.lat - clat) ** 2 + (l.lng - clng) ** 2),
                )
        if match is not None:
            day["hotel_lat"] = match.lat
            day["hotel_lng"] = match.lng
            day["hotel"] = match.name

    for day in plan["days"]:
        ids = [s["id"] for s in day.get("spots", [])]
        ordered = [catalog[i] for i in ids if i in catalog]
        if not ordered:
            continue
        day_start = _hhmm_to_min(day.get("day_start", "09:00"))
        _events, times = replay_day_events(city, ordered, day_start)
        for s in day["spots"]:
            if s["id"] in times:
                s["start"], s["end"] = times[s["id"]]
            else:
                name = s.get("name_en", s["id"])
                s["note"] = (s.get("note", "") + " · could not be visited in this day's window").strip(" ·")
                notes.append(f"{name}: not visitable within Day {day.get('day')}'s window — "
                             "moved out of the displayed times, consider another day")
    return plan, notes
