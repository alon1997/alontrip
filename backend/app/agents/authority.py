"""Engine-authoritative final plan.

The model orchestrates the conversation, but the itinerary shown to the
traveller is REBUILT from the engine's last draft (planner.py output) —
the same source of truth classic mode has always used. The model's
structured echo only contributes display notes. Hotels per night, day
counts, spot sets and visit times are deterministic engine output; model
drift (invented hotel names, duplicated spots, wrong day counts, impossible
times) cannot reach the screen.
"""

from __future__ import annotations

import logging

from .tools import load_last_draft, lodging_provider, poi_provider, replay_day_events

logger = logging.getLogger(__name__)


def _hhmm_to_min(v: str) -> int:
    try:
        h, m = v.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 9 * 60


def rebuild_from_engine(plan: dict) -> dict:
    """Return a plan whose days/hotels/spots/times all come from the last
    engine draft (file-backed stash). Falls back to patching the model's
    plan only when no draft exists."""
    draft = load_last_draft()
    if not draft or not draft.get("days"):
        logger.warning("rebuild_from_engine: no draft stashed — patching model plan")
        return _fallback_patch(plan)

    city = draft["city"]
    catalog = {p.id: p for p in poi_provider().get_pois(city)}

    # model notes per spot + day starts (display bits only)
    notes: dict[str, str] = {}
    starts: dict[int, str] = {}
    for d in plan.get("days", []):
        try:
            starts[int(d.get("day", 0))] = d.get("day_start", "09:00")
        except (TypeError, ValueError):
            pass
        for s in d.get("spots", []):
            if s.get("note"):
                notes[s["id"]] = s["note"]

    warnings: list[str] = list(draft.get("warnings", []))
    new_days = []
    for dd in draft["days"]:
        day_no = dd["day"]
        pois = [catalog[i] for i in dd["spot_ids"] if i in catalog]
        day_start = starts.get(day_no, "09:00")
        events, times = replay_day_events(
            city, pois, _hhmm_to_min(day_start), hotel_name=dd["hotel"]["name"]
        )
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
                "Couldn't fit in Day %s: %s — say the word and I'll re-plan"
                % (day_no, ", ".join(unvisitable))
            )
        # full day chain incl. hotel->first / last->hotel legs (classic's
        # schedule shape) — the day visibly starts and ends at the hotel.
        # Unvisitable waypoints (closed on arrival) stay in the chain but are
        # annotated, so nothing looks like a planned visit that isn't; a
        # hotel->hotel self-leg on an empty day is dropped.
        visited = set()
        for e in events:
            if e["kind"] == "visit":
                visited.add(e["title"])
        hotel_name = dd["hotel"]["name"]
        chain = []
        for e in events:
            if e["kind"] not in ("transit", "visit", "lunch", "dinner", "checkin"):
                continue
            title = e["title"]
            if e["kind"] == "transit":
                if " → " not in title:
                    chain.append(e)
                    continue
                src, dst = title.split(" → ", 1)
                if src == hotel_name and dst == hotel_name:
                    continue  # empty-day self leg
                marks = []
                for stop in (src, dst):
                    if stop != hotel_name and stop not in visited:
                        marks.append(stop)
                if marks:
                    title += "  · closed, skipped: " + ", ".join(marks)
            chain.append({
                "kind": e["kind"],
                "start": e["start"],
                "end": e["end"],
                "title": title,
                **({"summary": e["line_summary"]} if e.get("line_summary") else {}),
            })
        new_days.append({
            "day": day_no,
            "city": city,
            "day_start": day_start,
            "spots": spots,
            "chain": chain,
            "hotel": dd["hotel"]["name"],
            "hotel_lat": dd["hotel"]["lat"],
            "hotel_lng": dd["hotel"]["lng"],
            "summary": "",
        })

    logger.info("rebuild_from_engine: city=%s days=%d hotels=%s",
                city, len(new_days), [d["hotel"] for d in new_days])
    rebuilt = dict(plan)
    rebuilt["days"] = new_days
    rebuilt["warnings"] = warnings + [
        w for w in plan.get("warnings", []) if w not in warnings
    ][:2]
    return rebuilt


def _fallback_patch(plan: dict) -> dict:
    """Safety path: keep the model plan, attach hotel coords by name match /
    nearest-centroid fallback, and clip times per day."""
    if not plan.get("days"):
        return plan
    city = plan["days"][0].get("city", "")
    catalog = {p.id: p for p in poi_provider().get_pois(city)}
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
