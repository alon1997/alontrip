"""Engine-authoritative final plan.

The model orchestrates the conversation, but the itinerary shown to the
traveller is REBUILT from the engine's last draft (planner.py output) —
the same source of truth classic mode has always used. The model's
structured echo only contributes display notes. This makes hotels per day,
day counts, spot sets and visit times deterministic engine output; model
drift (invented hotel names, duplicated spots, wrong day counts, impossible
times) can no longer reach the screen.
"""

from __future__ import annotations

from . import tools as _tools
from .tools import poi_provider, replay_day_events


def _hhmm_to_min(v: str) -> int:
    try:
        h, m = v.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 9 * 60


def rebuild_from_engine(plan: dict) -> dict:
    """Return a plan whose days/hotels/spots/times all come from the last
    engine draft. Falls back to the model's plan (patched as before) only
    when no draft exists or the city changed."""
    draft = getattr(_tools, "_LAST_DRAFT", None)
    if not draft or not draft.get("days"):
        return _fallback_patch(plan)
    city = draft["city"]
    if plan.get("days") and plan["days"][0].get("city") not in (None, city):
        # traveller switched cities mid-conversation — trust the draft anyway
        pass

    catalog = {p.id: p for p in poi_provider().get_pois(city)}
    # model notes, keyed by spot id (nice-to-have only)
    notes: dict[str, str] = {}
    starts: dict[int, str] = {}
    for d in plan.get("days", []):
        starts[int(d.get("day", 0))] = d.get("day_start", "09:00")
        for s in d.get("spots", []):
            if s.get("note"):
                notes[s["id"]] = s["note"]

    warnings: list[str] = list(draft.get("warnings", []))
    new_days = []
    for dd in draft["days"]:
        day_no = dd["day"]
        pois = [catalog[i] for i in dd["spot_ids"] if i in catalog]
        day_start = starts.get(day_no, "09:00")
        _events, times = replay_day_events(city, pois, _hhmm_to_min(day_start))
        spots = []
        unvisitable = []
        for p in pois:
            t = times.get(p.id)
            if t is None:
                unvisitable.append(p.name_en or p.name)
                continue
            spots.append({
                "id": p.id,
                "name_en": p.name_en or p.name,
                "start": t[0],
                "end": t[1],
                "note": notes.get(p.id, ""),
            })
        if unvisitable:
            warnings.append(
                "Couldn't fit in Day %s: %s — say the word and I'll re-plan another day"
                % (day_no, ", ".join(unvisitable))
            )
        new_days.append({
            "day": day_no,
            "city": city,
            "day_start": day_start,
            "spots": spots,
            "hotel": dd["hotel"]["name"],
            "hotel_lat": dd["hotel"]["lat"],
            "hotel_lng": dd["hotel"]["lng"],
            "summary": "",
        })

    rebuilt = dict(plan)
    rebuilt["days"] = new_days
    # engine warnings first, then any short model caveats that still apply
    rebuilt["warnings"] = warnings + [
        w for w in plan.get("warnings", []) if w not in warnings
    ][:2]
    return rebuilt


def _fallback_patch(plan: dict) -> dict:
    """Old path (kept for safety): keep the model plan, attach hotel coords
    by name match / nearest-centroid fallback, and clip times per day."""
    if not plan.get("days"):
        return plan
    city = plan["days"][0].get("city", "")
    catalog = {p.id: p for p in poi_provider().get_pois(city)}
    from .tools import lodging_provider

    lodgings = lodging_provider().get_lodgings(city)
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
                    key=lambda l: (l.lat - clat) ** 2 + (l.lng - clng) ** 2,
                )
        if match is not None:
            day["hotel_lat"] = match.lat
            day["hotel_lng"] = match.lng
            day["hotel"] = match.name

    notes: list[str] = []
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
                notes.append(f"{name}: not visitable within Day {day.get('day')}'s window")
    if notes:
        plan.setdefault("warnings", []).extend(notes[:3])
    return plan
