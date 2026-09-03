# Timed itinerary for the result sidebar (T-023).
# Pure display helper: does not call SerpApi / DeepSeek.
# Clock starts 09:00; intercity legs jump to 16:00 if still morning;
# lunch 60 min around noon (do not leave a <=15 min visit stub);
# dinner 90 min in 17:30–19:30, after the last POI or back at the hotel.

from __future__ import annotations

from .currency import DISPLAY_CURRENCY, to_usd
from .hours import clip_visit
from .poi import Poi
from .transit import TransitRoute

CHECKIN_MIN = 20
LUNCH_MIN = 60
DINNER_MIN = 90
DAY_START_MIN = 9 * 60
INTERCITY_MIN = 16 * 60
LUNCH_WINDOW_START = 11 * 60 + 30
LUNCH_NOON = 12 * 60
LUNCH_WINDOW_END = 14 * 60
# T-040 (user rule, 2026-08-28): dinner targets 18:00. A day whose last
# spot ends early waits until 18:00; a late finish eats right away (the old
# 19:30 upper window is gone — late days just eat late, still under the cap).
DINNER_EARLIEST = 18 * 60
# Flight times (T-034, user-confirmed defaults): minutes after landing before
# sightseeing can start (immigration + bags + into the city), and minutes
# before takeoff by which the last day must reach the airport (check-in +
# security). DAY_END_MIN is the hard "sightseeing over" line for every day.
LANDING_BUFFER_MIN = 90
TAKEOFF_BUFFER_MIN = 120
DAY_END_MIN = 22 * 60


def _clock(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _line_summary(route: TransitRoute | None) -> str:
    if route is None:
        return "Transit (pending)"
    if any(leg.travel_mode == "taxi" for leg in route.legs):
        return "No public transit · taxi"
    if route.estimated:
        walking = any(leg.travel_mode == "walking" for leg in route.legs)
        return "Walk" if walking and route.total_cost == 0 else "No public transit · taxi"
    names = [leg.line_name for leg in route.legs if leg.travel_mode == "transit" and leg.line_name]
    if names:
        return " → ".join(names)
    if any(leg.travel_mode == "walking" for leg in route.legs):
        return "Walk"
    return "No public transit · taxi"


def build_day_schedule(
    *,
    chain: list[tuple[str, str, float, float, str | None, str]],
    legs: list,
    poi_by_id: dict[str, Poi],
    is_first_day: bool,
    has_arrival_hub: bool,
    node_labels: dict[tuple[str, str], str],
    day_start_min: int | None = None,
    day_end_cutoff_min: int | None = None,
) -> list[dict]:
    """``chain`` items are (kind, id, lat, lng, role, city). ``legs[i]`` is the
    transit for chain[i] → chain[i+1] (same order as optimize-route).

    ``day_start_min`` (T-034): arrival-day clock start — landing time plus
    LANDING_BUFFER_MIN; ``None`` keeps the 09:00 default. ``day_end_cutoff_min``
    (T-034): departure-day latest sightseeing end (takeoff minus
    TAKEOFF_BUFFER_MIN); always combined with the 22:00 hard cap.
    """
    events: list[dict] = []
    clock = day_start_min if day_start_min is not None else DAY_START_MIN
    cap = DAY_END_MIN if day_end_cutoff_min is None else min(DAY_END_MIN, day_end_cutoff_min)
    lunch_done = False
    dinner_done = False
    poi_count = sum(1 for item in chain if item[0] == "poi")

    def label(kind: str, ident: str) -> str:
        return node_labels.get((kind, ident), ident)

    def add_dinner() -> None:
        nonlocal clock, dinner_done
        if clock >= cap:
            dinner_done = True  # past the day's cutoff — genuinely no time
            return
        events.append({
            "kind": "dinner",
            "start": _clock(clock),
            "end": _clock(clock + DINNER_MIN),
            "duration_min": DINNER_MIN,
            "title": "Dinner",
        })
        clock += DINNER_MIN
        dinner_done = True

    for i, leg in enumerate(legs):
        from_kind, from_id, _a, _b, _c, from_city = chain[i]
        to_kind, to_id, _d, _e, to_role, to_city = chain[i + 1]
        intercity = from_city != to_city
        if intercity and clock < INTERCITY_MIN:
            clock = INTERCITY_MIN

        route: TransitRoute | None = getattr(leg, "route", None)
        dur = route.total_duration_min if route is not None else 15
        estimated = bool(route.estimated) if route is not None else True
        modes = [rleg.travel_mode for rleg in route.legs] if route else []
        walk_only = bool(modes) and all(m == "walking" for m in modes)
        cost = float(route.total_cost) if route is not None else None
        if walk_only:
            cost = 0.0
        currency = DISPLAY_CURRENCY if cost is not None else None
        events.append({
            "kind": "transit",
            "start": _clock(clock),
            "end": _clock(clock + dur),
            "duration_min": dur,
            "title": f"{label(from_kind, from_id)} → {label(to_kind, to_id)}",
            "line_summary": _line_summary(route),
            "cost": cost,
            "currency": currency,
            "estimated": estimated,
            "data_source": getattr(route, "data_source", "") if route is not None else "",
        })
        clock += dur

        still_has_poi = any(item[0] == "poi" for item in chain[i + 2 :])
        if (
            is_first_day
            and has_arrival_hub
            and to_kind == "lodging"
            and still_has_poi
        ):
            events.append({
                "kind": "checkin",
                "start": _clock(clock),
                "end": _clock(clock + CHECKIN_MIN),
                "duration_min": CHECKIN_MIN,
                "title": "Hotel check-in / drop bags",
            })
            clock += CHECKIN_MIN

        if to_kind == "poi":
            poi = poi_by_id.get(to_id)
            remain = poi.suggested_duration_min if poi is not None else 90
            raw_ticket = poi.ticket_price if poi is not None else None
            ticket = to_usd(raw_ticket, poi.ticket_currency if poi is not None else None)
            tcur = DISPLAY_CURRENCY if ticket is not None else None
            visit_name = (poi.name_en or poi.name) if poi is not None else to_id
            clock, remain = clip_visit(
                clock, remain, poi.opening_hours if poi is not None else None
            )
            # Hard day cap (22:00, or the departure-day flight cutoff):
            # never start or extend a visit past it.
            if clock >= cap:
                remain = 0
            else:
                remain = min(remain, cap - clock)

            more_pois = any(item[0] == "poi" for item in chain[i + 2 :])
            while remain > 0:
                if (
                    not lunch_done
                    and clock < LUNCH_WINDOW_END
                    and clock + remain >= LUNCH_WINDOW_START
                ):
                    before = max(0, min(remain, LUNCH_NOON - clock))
                    after = remain - before
                    if before > 15 and after > 15:
                        events.append({
                            "kind": "visit",
                            "start": _clock(clock),
                            "end": _clock(clock + before),
                            "duration_min": before,
                            "title": visit_name,
                            "cost": ticket,
                            "currency": tcur,
                        })
                        clock += before
                        remain -= before
                    elif after <= 15:
                        events.append({
                            "kind": "visit",
                            "start": _clock(clock),
                            "end": _clock(clock + remain),
                            "duration_min": remain,
                            "title": visit_name,
                            "cost": ticket,
                            "currency": tcur,
                        })
                        clock += remain
                        remain = 0
                    events.append({
                        "kind": "lunch",
                        "start": _clock(clock),
                        "end": _clock(clock + LUNCH_MIN),
                        "duration_min": LUNCH_MIN,
                        "title": "Lunch",
                    })
                    clock += LUNCH_MIN
                    lunch_done = True
                    continue
                events.append({
                    "kind": "visit",
                    "start": _clock(clock),
                    "end": _clock(clock + remain),
                    "duration_min": remain,
                    "title": visit_name,
                    "cost": ticket,
                    "currency": tcur,
                })
                clock += remain
                remain = 0
            if not dinner_done and not more_pois:
                if clock < DINNER_EARLIEST:
                    clock = DINNER_EARLIEST
                add_dinner()  # skipped entirely when it would run past the cap

    ends_at_hub = bool(chain) and chain[-1][0] == "hub"
    if poi_count and not dinner_done and not ends_at_hub:
        if clock < DINNER_EARLIEST:
            clock = DINNER_EARLIEST
        add_dinner()  # no-op when it would run past the cap

    return events
