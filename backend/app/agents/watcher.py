"""Background trip watcher (T-110) — the product's differentiator.

Reality changes under a booked trip: a temple closes early "today", rain
shuts a viewpoint, a flight moves. The watcher does NOT regenerate the
whole trip; it replays the affected days against real transit times with
the new constraint, tries the smallest fixes first (in-day reorder → move
to another day), and returns ONE notification: either "auto-fixed, here's
what changed" or a single decision push in the same shape as the agent's
ask_traveller gates.

Deterministic and free: pure engine, no LLM in the loop.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from alontrip.services.hours import parse_hours
from alontrip.services.poi import Poi
from alontrip.services.schedule import build_day_schedule
from alontrip.services.transit import Coord, LocalTransitProvider

from agent.tools import DATA, _lodgings, _pois, _transit


@dataclass
class Event:
    poi_id: str
    closes: str  # "HH:MM" — the spot's closing time "today"

    @property
    def closes_min(self) -> int:
        h, m = self.closes.split(":")
        return int(h) * 60 + int(m)


@dataclass
class WatchResult:
    auto_fixed: list[str] = field(default_factory=list)
    decision: dict | None = None  # same shape as ask_traveller's interrupt reason
    changed_days: list[int] = field(default_factory=list)

    @property
    def needs_decision(self) -> bool:
        return self.decision is not None


class _Leg:
    """engine's build_day_schedule reads .route off each leg."""

    def __init__(self, route):
        self.route = route


def _replay(pois: list[Poi], city: str) -> tuple[list[dict], set[str]]:
    """Timed schedule for one day; returns (events, visited_ids)."""
    lodging = _lodgings.get_lodgings(city)[0]
    labels = {("poi", p.id): p.name_en or p.name for p in pois}
    chain = [("lodging", lodging.id, lodging.lat, lodging.lng, "start", city)] + [
        ("poi", p.id, p.lat, p.lng, None, city) for p in pois
    ] + [("lodging", lodging.id, lodging.lat, lodging.lng, "end", city)]
    legs = [
        _Leg(_transit.get_route(Coord(lat=a.lat, lng=a.lng), Coord(lat=b.lat, lng=b.lng), city=city))
        for a, b in zip([lodging, *pois], [*pois, lodging])
    ]
    events = build_day_schedule(
        chain=chain, legs=legs, poi_by_id={p.id: p for p in pois},
        is_first_day=False, has_arrival_hub=False, node_labels=labels,
    )
    visited = {e["title"] for e in events if e["kind"] == "visit"}
    return events, visited


def _day_pois(day: dict, city: str) -> list[Poi]:
    by_id = {p.id: p for p in _pois.get_pois(city)}
    return [by_id[s["id"] if isinstance(s, dict) else s] for s in day.get("spots", [])]


def _misses(day: dict, city: str, events: list[dict], visited: set[str]) -> list[str]:
    return [
        p.id for p in _day_pois(day, city)
        if (p.name_en or p.name) not in visited
    ]


def watch_trip(city: str, day_plans: list[dict], events: list[Event]) -> WatchResult:
    """Re-plan around ``events`` over ``day_plans`` (draft_day_plan shape)."""
    result = WatchResult()
    catalog = {p.id: p for p in _pois.get_pois(city)}
    overrides = {e.poi_id: e for e in events}

    def with_overrides(pois: list[Poi]) -> list[Poi]:
        # NOTE: the engine's parse_hours only reads AM/PM after "Closes"
        # (24h "Closes 17:00" parses as unknown → treated open), so inject
        # overrides in the format it understands. Logged in engine-issues.
        def ampm(hhmm: str) -> str:
            h, m = (int(x) for x in hhmm.split(":"))
            return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"

        out = []
        for p in pois:
            ev = overrides.get(p.id)
            if ev is None:
                out.append(p)
                continue
            patched = copy.deepcopy(p)
            patched.opening_hours = f"Open Closes {ampm(ev.closes)}"
            out.append(patched)
        return out

    # 1) who got squeezed out by today's news?
    squeezed: dict[int, list[str]] = {}
    for i, day in enumerate(day_plans):
        if not any(overrides.get(s["id"] if isinstance(s, dict) else s) for s in day.get("spots", [])):
            continue
        _ev, visited = _replay(with_overrides(_day_pois(day, city)), city)
        squeezed[i] = _misses(day, city, _ev, visited)

    if not squeezed:
        result.auto_fixed.append("No spot affected by today's change — trip untouched.")
        return result

    # 2) smallest fix: reorder the affected day early-closing-first and replay
    for i, missed in squeezed.items():
        if not missed:
            continue
        day = day_plans[i]
        pois = with_overrides(_day_pois(day, city))
        pois.sort(key=lambda p: parse_hours(p.opening_hours)[1] or 24 * 60)
        _ev, visited = _replay(pois, city)
        still = [m for m in missed if catalog[m].name_en not in visited and catalog[m].name not in visited]
        if not still:
            for m in missed:
                result.auto_fixed.append(
                    f"{catalog[m].name_en}: moved earlier in Day {i + 1} — fits before today's early close."
                )
            result.changed_days.append(i)
            day["spots"] = [{"id": p.id, "name_en": p.name_en or p.name} for p in pois]
            squeezed[i] = []
    squeezed = {i: m for i, m in squeezed.items() if m}
    if not squeezed:
        return result

    # 3) still broken: try moving the missed spots to another day that fits
    for i, missed in squeezed.items():
        for m in missed:
            placed = False
            for j, other in enumerate(day_plans):
                if j == i:
                    continue
                trial = with_overrides(_day_pois(other, city)) + [catalog[m]]
                _ev, visited = _replay(trial, city)
                if catalog[m].name_en in visited or catalog[m].name in visited:
                    day_plans[j]["spots"] = [{"id": p.id, "name_en": p.name_en or p.name} for p in trial]
                    result.auto_fixed.append(
                        f"{catalog[m].name_en}: Day {i + 1} can't hold it today → moved to Day {j + 1}."
                    )
                    result.changed_days += [i, j]
                    placed = True
                    break
            if not placed:
                opts = [
                    f"Drop {catalog[m].name_en} for this trip",
                    f"Visit {catalog[m].name_en} on a later day (add a day)",
                    f"Keep it but accept a too-short visit on Day {i + 1}",
                ]
                result.decision = {
                    "question": f"Today {catalog[m].name_en} closes at {overrides[m].closes} — "
                                f"no re-ordering fits it. What should I do?",
                    "options": opts,
                    "context": f"Event watcher: {catalog[m].name_en} closing early.",
                }
    return result


if __name__ == "__main__":
    # Demo: the trip from demo_interrupt's final shape; today Kiyomizu-dera
    # shut for a private event until 08:30 — i.e. effectively closed all day.
    from agent.tools import draft_day_plan

    trip = draft_day_plan(
        "kyoto", 2,
        ["kinkaku-ji", "kiyomizu-dera", "gion", "sannenzaka", "fushimi-inari", "nishiki-market"],
        arrival_hub_id="kansai-airport", arrival_time="15:30",
        first_day_density="few", last_day_density="few",
    )
    assert "error" not in trip, trip
    print("trip before event:")
    for d in trip["days"]:
        print(f"  Day {d['day']}: {[s['name_en'] for s in d['spots']]}")

    watch = watch_trip("kyoto", trip["days"], [Event("kiyomizu-dera", "08:30")])
    print("\n🔔 watcher push:")
    for line in watch.auto_fixed:
        print(f"  ✅ {line}")
    if watch.decision:
        print(f"  ⏸ {watch.decision['question']}")
        for k, o in enumerate(watch.decision["options"], 1):
            print(f"     {k}. {o}")
    else:
        print("  (no decision needed — trip already re-planned)")
