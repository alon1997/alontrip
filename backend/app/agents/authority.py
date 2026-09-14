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
    arrival_hub = draft.get("arrival_hub")
    departure_hub = draft.get("departure_hub")
    n_days = len(draft["days"])
    new_days = []
    for dd in draft["days"]:
        day_no = dd["day"]
        pois = [catalog[i] for i in dd["spot_ids"] if i in catalog]
        day_start = starts.get(day_no, "09:00")
        day_arrival = arrival_hub if day_no == 1 else None
        day_departure = departure_hub if day_no == n_days else None
        # classic rule: no dinner after the departure airport leg
        events, times = replay_day_events(
            city, pois, _hhmm_to_min(day_start),
            hotel_name=dd["hotel"]["name"],
            arrival_hub=day_arrival, departure_hub=day_departure,
        )
        # T-A9: a spot schedule.py refused to visit is dropped from the day —
        # "couldn't be visited" is an engine failure to fix, never a
        # traveller-facing warning, and its ghost legs leave with it
        spots = []
        unvisited_titles = set()
        for p in pois:
            t = times.get(p.id)
            if t is None:
                unvisited_titles.add(p.name_en or p.name)
                continue
            spots.append({
                "id": p.id,
                "name_en": p.name_en or p.name,
                "start": t[0],
                "end": t[1],
                "note": notes.get(p.id, ""),
            })
        # full day chain incl. hotel->first / last->hotel legs (classic's
        # schedule shape) — the day visibly starts and ends at the hotel.
        # A hotel->hotel self-leg on an empty day is dropped.
        hub_names = {h["name"] for h in (draft.get("arrival_hub"), draft.get("departure_hub")) if h}
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
                if any(stop in unvisited_titles for stop in (src, dst)):
                    continue  # ghost leg touching a spot that never got a visit
            chain.append({
                "kind": e["kind"],
                "start": e["start"],
                "end": e["end"],
                "title": title,
                **({"summary": e["line_summary"]} if e.get("line_summary") else {}),
            })
        if day_departure is not None:
            # classic rule: nothing after the departure hub — drop the
            # hotel-return dinner so the airport leg closes the day
            chain = [e for e in chain if e.get("kind") != "dinner"]
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

    for day in plan["days"]:
        ids = [s["id"] for s in day.get("spots", [])]
        ordered = [catalog[i] for i in ids if i in catalog]
        if not ordered:
            continue
        day_start = _hhmm_to_min(day.get("day_start", "09:00"))
        _events, times = replay_day_events(city, ordered, day_start)
        # T-A9: a spot the replay cannot visit is dropped from the day —
        # never annotated on the row, never warned about
        kept = []
        for s in day.get("spots", []):
            if s["id"] in times:
                s["start"], s["end"] = times[s["id"]]
                kept.append(s)
            else:
                logger.info(
                    "fallback: %s not visitable in day %s window — dropped",
                    s["id"], day.get("day"),
                )
        day["spots"] = kept
    return plan
