"""Rule-based itinerary planner v1 (T-010, plan doc 6.1b).

Turns a multi-city trip request into a day-by-day plan: which city each day
belongs to, which POIs happen that day (ordered), and which lodging that
night — everything `POST /optimize-route` needs before transit legs get
queried. DeepSeek per-city filling (2.8) is T-011: this module both is the
fallback when the model is unavailable and defines the shape/validation the
model's JSON must satisfy.

All 400 `detail` strings here are frozen by implementation batches doc 2.6
(the frontend matches on them, so don't reword).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from .grouping import (
    DEEPSEEK_TIMEOUT_S,
    RuleBasedGrouper,
    _content_block_types,
    _extract_json,
    _find_text_block,
    deepseek_messages_body,
)
from .hours import parse_hours
from .lodging import Lodging
from .poi import Poi
from .schedule import (
    DAY_END_MIN,
    DAY_START_MIN,
    DINNER_MIN,
    INTERCITY_MIN,
    LANDING_BUFFER_MIN,
    LUNCH_MIN,
    TAKEOFF_BUFFER_MIN,
)
from .transit import haversine_km
from .transport_hub import TransportHub

logger = logging.getLogger(__name__)

# Below this distance, "switching hotels" isn't worth the hassle for a
# system_multi traveller — keep last night's hotel instead (6.1b D).
HOTEL_STICKINESS_KM = 2.0

MAX_CITIES = 4
MIN_DAYS = 2
MAX_DAYS = 14

# Arrival/departure days stay light: none = 0 POIs; few = as few as the
# rest of the trip can reasonably absorb (prefer 1, not a hard cap of 1).
# Packed ``edge_density`` strings are the T-016 shape; new callers send
# first_day_density / last_day_density independently (including none+none).
EDGE_DENSITY_MODES: dict[str, tuple[str, str]] = {
    "first_few_last_none": ("few", "none"),
    "first_none_last_few": ("none", "few"),
    "first_few_last_few": ("few", "few"),
    "first_none_last_none": ("none", "none"),
}
VALID_EDGE_MODES = {"none", "few"}
DEFAULT_EDGE_DENSITY = "first_few_last_none"
DEFAULT_FIRST_DAY_DENSITY = "few"
DEFAULT_LAST_DAY_DENSITY = "none"
IDEAL_FEW_POIS = 1
# Parks / mountains that fill a calendar day. Do not mix with other POIs.
FULL_DAY_DURATION_MIN = 360
DAYLIGHT_BUDGET_MIN = 9 * 60
TRANSIT_SLACK_PER_HOP_MIN = 25
# T-048: comfortable mid-trip pace = 3 spots/day (same bar as
# _comfortable_spot_budget). Only drives edge-day rebalance counts on the
# rule path; the real hard constraints are budget packing + the closing-time
# final check.
REASONABLE_DAY_POIS = 3
# T-045: at planner time the arrival day's first leg has no real duration
# (legs are queried by main.py), so a flat 25 min/hop approximation blesses
# phantom "16:20 arrival at Ueno Park" plans (real 18:40, already closed).
# The final check's simulation uses a conservative estimate instead: 20 min
# check-in + 1.2 min/km airport rail — Narita -> city 60 km ~= 92 min vs
# 99 min measured, right order of magnitude. Better to move spots to another
# day (harmless) than leave an "arrive after closing" spot on the arrival day.
_CHECKIN_MIN = 20
_COMMUTE_BASE_MIN = 20.0
_COMMUTE_PER_KM_MIN = 1.2
# Airport -> city does not follow straight-line distance: Narita -> Akihabara
# is only 17 km straight-line (the formula says 40 min) but Narita Express
# takes 99 min. Airports always use a flat conservative 90 min; stations use
# the straight-line formula.
_AIRPORT_COMMUTE_MIN = 90.0
# T-051: arrival-day geographic anchoring - on landing evening only spots
# within 5km of the hotel count as "on the way"; anything farther moves to
# the day it geographically belongs to (an 8km cross-city detour on arrival
# night was the harshest review this product ever received).
_ARRIVAL_PROXIMITY_KM = 5.0
# T-052: repair anchoring - a spot the model missed follows its geographically
# closest placed sibling (<=8km) onto the same day, so far-suburb clusters
# (big park + cable car) stick together instead of being scattered into
# downtown days by centroid arithmetic.
_REPAIR_ANCHOR_KM = 8.0
# Fixed overhead on an intercity day after the 16:00 station arrival, before
# sightseeing really starts (baggage storage / transfers / first leg into town)
_INTERCITY_ARRIVAL_BUFFER_MIN = 60
# T-054: tightened from 8 km — on a 3-spot day a far spot drags its own day's
# centroid toward itself (monkey park on a day with Sannenzaka + Kiyomizu-dera
# sat only ~7 km from its own centroid and escaped the old gate). The real
# guard is still the 2 km margin below: a move fires only when another day is
# *dominantly* closer, so boundary spots on contiguous chains don't churn.
DISTRICT_OUTLIER_KM = 4.0
DISTRICT_CLOSER_OTHER_KM = 2.0


def _day_geo_key(group: list[Poi], mover: Poi) -> tuple[float, int]:
    """Sort key for choosing a destination day: distance from the mover to
    the day's centroid first, load as tie-break. Empty days sort last —
    they are a last resort, never an attractive "0 load" dump site."""
    if not group:
        return (float("inf"), 0)
    clat, clng = _centroid(group)
    return (haversine_km(mover.lat, mover.lng, clat, clng), _day_load_min(group))


def resolve_edge_modes(
    first_day_density: str | None,
    last_day_density: str | None,
    edge_density: str | None,
) -> tuple[str, str]:
    """Prefer the two independent switches; fall back to the packed T-016 field."""
    if first_day_density is not None or last_day_density is not None:
        first = (first_day_density or DEFAULT_FIRST_DAY_DENSITY).strip().lower()
        last = (last_day_density or DEFAULT_LAST_DAY_DENSITY).strip().lower()
        if first not in VALID_EDGE_MODES or last not in VALID_EDGE_MODES:
            raise PlanningError("first_day_density and last_day_density must be none or few")
        return first, last
    packed = edge_density or DEFAULT_EDGE_DENSITY
    if packed not in EDGE_DENSITY_MODES:
        raise PlanningError(f"edge_density must be one of {sorted(EDGE_DENSITY_MODES)}")
    return EDGE_DENSITY_MODES[packed]


class PlanningError(Exception):
    """Carries one of the frozen 400 `detail` strings straight to the caller."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


@dataclass
class CustomStayInput:
    lodging_id: str
    days: list[int]


@dataclass
class PlannedDay:
    day: int
    city: str
    pois: list[Poi]          # ordered within the day (C)
    lodging: Lodging         # night stay (D)
    morning_lodging: Lodging  # where the day started (E) — usually == lodging


@dataclass
class TripPlan:
    days: list[PlannedDay]
    unselected_poi_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Honest report of what actually ran (T-011): "deepseek" only if every
    # city's day-filling came from the model, "rule-based" only if none did
    # (including the no-key case — DeepSeek is never even called then),
    # "mixed" when some cities used the model and others fell back.
    grouper: str = "rule-based"
    # T-016: set only when the traveller picked an arrival/departure hub —
    # main.py's chain builder swaps these in for day 1's start / the last
    # day's end instead of a hotel node.
    arrival_hub: TransportHub | None = None
    departure_hub: TransportHub | None = None


def _centroid(pois: list[Poi]) -> tuple[float, float]:
    return (sum(p.lat for p in pois) / len(pois), sum(p.lng for p in pois) / len(pois))


def _hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _flight_day_budgets(
    n_days: int,
    owns_global_first_day: bool,
    owns_global_last_day: bool,
    arrival_start_min: int | None,
    departure_cutoff_min: int | None,
) -> list[int]:
    """Per-day load budgets (T-034). The comfort target stays ~9 h, but the
    arrival day is capped at 22:00 minus its late start, and the departure
    day at the flight cutoff minus the 09:00 start. Floors at 30 min so a
    pathological 23:50 landing still yields a (tiny) valid budget."""
    budgets = [DAYLIGHT_BUDGET_MIN] * n_days
    floor = 30
    if owns_global_first_day and arrival_start_min is not None:
        budgets[0] = min(budgets[0], max(DAY_END_MIN - arrival_start_min, floor))
    if owns_global_last_day and departure_cutoff_min is not None:
        budgets[-1] = min(budgets[-1], max(departure_cutoff_min - DAY_START_MIN, floor))
    return budgets


def _nearest_lodging(lat: float, lng: float, candidates: list[Lodging]) -> Lodging:
    return min(candidates, key=lambda l: haversine_km(lat, lng, l.lat, l.lng))


def _split_city_days(cities: list[str], days: int, poi_counts: dict[str, int]) -> dict[str, list[int]]:
    """6.1b A: each city gets >=1 day; the rest is handed out proportionally
    to how many POIs were selected there, by largest-remainder rounding."""
    n = len(cities)
    extra = days - n
    weights = [max(poi_counts.get(c, 0), 1) for c in cities]  # never divide by zero
    total = sum(weights)
    allocations = [1] * n
    if extra > 0:
        raw_shares = [extra * w / total for w in weights]
        floor_shares = [int(s) for s in raw_shares]
        remainder = extra - sum(floor_shares)
        # Largest fractional part first — the standard largest-remainder method.
        order = sorted(range(n), key=lambda i: raw_shares[i] - floor_shares[i], reverse=True)
        for i in order[:remainder]:
            floor_shares[i] += 1
        allocations = [1 + s for s in floor_shares]

    result: dict[str, list[int]] = {}
    cursor = 1
    for city, count in zip(cities, allocations):
        result[city] = list(range(cursor, cursor + count))
        cursor += count
    return result


def _group_city_pois(pois: list[Poi], n_days: int) -> list[list[Poi]]:
    """Group this city's spots into ``n_days`` day-lists via a greedy
    nearest-neighbour chain (T-052): repeatedly walk to the nearest
    unassigned spot from the current chain end — geography stays contiguous
    by construction, so a day never holds two far-apart districts.

    Fewer spots than days (a demo edge case): each spot gets its own day,
    spread across the block, remaining days stay empty (pure hotel/transit) —
    an earlier version cycled back through the same spots, which meant
    re-visiting a spot on a later day (bug report 2026-08-21)."""
    # T-052 core: greedy nearest-neighbour chain - start at the spot closest
    # to the city centroid, repeatedly walk to the nearest unassigned spot,
    # then cut the chain into n_days contiguous segments. Chain grouping
    # guarantees geographic contiguity whether spots outnumber days or not.
    if not pois:
        return [[] for _ in range(n_days)]
    c = _centroid(pois)
    cur = min(pois, key=lambda p: haversine_km(c[0], c[1], p.lat, p.lng))
    chain: list[Poi] = [cur]
    remaining = [p for p in pois if p.id != cur.id]
    while remaining:
        nxt = min(remaining, key=lambda p: haversine_km(chain[-1].lat, chain[-1].lng, p.lat, p.lng))
        chain.append(nxt)
        remaining.remove(nxt)
    groups: list[list[Poi]] = []
    base, extra = divmod(len(chain), n_days)
    idx = 0
    for _ in range(n_days):
        size = base + (1 if _ < extra else 0)
        groups.append(chain[idx:idx + size])
        idx += size
    return groups


def _day_load_min(pois: list[Poi]) -> int:
    """Daily load: stays + hops + both meals. T-048: hops now use the
    distance-calibrated formula (fitted on 163 real cache entries) instead
    of a flat 25 min — the flat value under-measured Tokyo (real median
    41 min for 6-10 km hops) and let days silently hold a 5th spot. The
    packer now speaks the same units as the terminal check's simulator:
    ~3 comfortable spots/day."""
    if not pois:
        return 0
    stay = sum(p.suggested_duration_min or 90 for p in pois)
    hops = sum(_hop_min(a.lat, a.lng, b.lat, b.lng) for a, b in zip(pois, pois[1:]))
    if hops:
        hops += TRANSIT_SLACK_PER_HOP_MIN  # hotel -> first stop hop; keep a 25 min floor
    return stay + hops + LUNCH_MIN + DINNER_MIN


def _pack_day_capacity(
    groups: list[list[Poi]],
    empty_idx: set[int] | frozenset[int] | None = None,
    budget_by_idx: list[int] | None = None,
) -> list[list[Poi]]:
    """Move overflow spots so each day fits its load budget.

    ``empty_idx`` are days that must stay empty (arrival/departure ``none``).
    Overflow never lands on those days. ``budget_by_idx`` (T-034) lets the
    arrival/departure days run on shorter flight-aware budgets; days without
    an entry keep the ~9 hour comfort budget. Moves go to the lightest
    allowed day; stop when a move would not reduce the source day's load peak.
    """
    empty = set(empty_idx or ())
    budgets = list(budget_by_idx or [])
    packed = [list(group) for group in groups]
    n = len(packed)

    def budget_of(i: int) -> int:
        return budgets[i] if i < len(budgets) else DAYLIGHT_BUDGET_MIN

    def open_days() -> list[int]:
        return [j for j in range(n) if j not in empty]

    def last_movable(group: list[Poi]) -> int | None:
        return next(
            (
                j
                for j in range(len(group) - 1, -1, -1)
                if (group[j].suggested_duration_min or 0) < FULL_DAY_DURATION_MIN
            ),
            None,
        )

    for i in sorted(empty):
        while packed[i]:
            dests = open_days()
            if not dests:
                break
            dest = min(dests, key=lambda j: abs(j - i))
            mover = packed[i].pop(0)
            if dest > i:
                packed[dest].insert(0, mover)
            else:
                packed[dest].append(mover)

    blocked: set[int] = set()
    for _ in range(sum(len(g) for g in packed) + 1):
        overloaded = [
            i for i in open_days()
            if i not in blocked
            and _day_load_min(packed[i]) > budget_of(i)
            and len(packed[i]) > 1
        ]
        if not overloaded:
            break
        src = max(overloaded, key=lambda i: _day_load_min(packed[i]))
        idx = last_movable(packed[src])
        if idx is None:
            blocked.add(src)
            continue
        mover = packed[src][idx]
        # T-045: a destination day must have room (including its own budget)
        # before a move is allowed — comparing load alone made the arrival day
        # (budget 365), the lightest day, the overflow dumping ground where
        # "arrive after closing" spots got parked (2026-08-28 prod: a museum
        # as the day's last stop at 21:00).
        dests = [
            j for j in open_days()
            if j != src and _day_load_min(packed[j] + [mover]) <= budget_of(j)
        ]
        if not dests:
            blocked.add(src)
            continue
        # T-054: nearest day wins, not lightest day — the lightest choice kept
        # scattering spots across town (Daikaku-ji next to Sento Palace) after
        # the NN chain had built geographically contiguous days.
        dest = min(dests, key=lambda j: _day_geo_key(packed[j], mover))
        src_load = _day_load_min(packed[src])
        dest_load_after = _day_load_min(packed[dest] + [mover])
        if dest_load_after >= src_load:
            blocked.add(src)
            continue
        packed[src].pop(idx)
        if dest > src:
            packed[dest].insert(0, mover)
        else:
            packed[dest].append(mover)
    return packed


def _empty_day_indices(
    n_days: int,
    first_mode: str | None,
    last_mode: str | None,
) -> set[int]:
    if n_days <= 1:
        return set()
    if first_mode == "none" and last_mode == "none" and n_days == 2:
        return set()
    empty: set[int] = set()
    if first_mode == "none":
        empty.add(0)
    if last_mode == "none":
        empty.add(n_days - 1)
    return empty


def _districts_ok(days: list[list[Poi]]) -> bool:
    """Reject a table that parked a point with a far-away day instead of its district."""
    centroids: list[tuple[float, float] | None] = [
        _centroid(group) if group else None for group in days
    ]
    for i, group in enumerate(days):
        if len(group) < 2 or centroids[i] is None:
            continue
        clat, clng = centroids[i]
        for poi in group:
            dist_own = haversine_km(poi.lat, poi.lng, clat, clng)
            if dist_own <= DISTRICT_OUTLIER_KM:
                continue
            for j, other in enumerate(days):
                if i == j or not other or centroids[j] is None:
                    continue
                dist_other = haversine_km(poi.lat, poi.lng, *centroids[j])
                if dist_own - dist_other > DISTRICT_CLOSER_OTHER_KM:
                    return False
    return True


def _insert_best_position(day: list[Poi], poi: Poi) -> None:
    """Insert ``poi`` at the chain position adding the least travel distance."""
    if not day:
        day.append(poi)
        return
    best_k, best_cost = 0, None
    for k in range(len(day) + 1):
        trial = day[:k] + [poi] + day[k:]
        cost = sum(
            haversine_km(a.lat, a.lng, b.lat, b.lng) for a, b in zip(trial, trial[1:])
        )
        if best_cost is None or cost < best_cost:
            best_k, best_cost = k, cost
    day.insert(best_k, poi)


def _norm_pid(pid: str) -> str:
    """ids down to [a-z0-9] so "Edo-Tokyo_OpenAir" style near-misses compare."""
    return re.sub(r"[^a-z0-9]", "", str(pid).lower())


def _pid_tokens(pid: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(pid).lower()) if t]


def _tokens_in_order(sub: list[str], seq: list[str]) -> bool:
    """Every token of ``sub`` appears in ``seq`` in order (greedy scan)."""
    it = iter(seq)
    return all(tok in it for tok in sub)


def _repair_poi_ids(raw_days: list, by_id: dict[str, Poi]) -> list[str] | None:
    """Fix near-miss poi ids in place instead of dropping the whole table.

    With long spot lists the model sometimes mis-echoes an id (prod
    2026-08-28, 17-spot tokyo: "unknown or duplicate poi id" killed
    otherwise-good tables twice). A returned id resolves when it matches
    exactly ONE catalog id after normalization ([a-z0-9] squeeze): equal,
    contained as a substring on either side, or its hyphen-tokens appear
    in order in the catalog id ("edo-tokyo-open-air-museum" repairs to
    "...-architectural-..."; dropped middle words are fine, reordered or
    foreign words are not). Ambiguous or unresolvable → ``None`` and the
    caller falls back to rule-based as before. Same-day duplicates are
    deduped keeping the first; cross-day ones stay for
    :func:`_repair_poi_set`."""
    catalog = [(pid, _norm_pid(pid), _pid_tokens(pid)) for pid in by_id]
    notes: list[str] = []
    for entry in raw_days:
        if not isinstance(entry, dict):
            continue
        fixed: list[str] = []
        for pid in entry.get("poi_ids") or []:
            if pid in by_id:
                fixed.append(pid)
                continue
            norm, tokens = _norm_pid(pid), _pid_tokens(pid)
            if not norm:
                return None
            matches = [
                canon
                for canon, c_norm, c_tokens in catalog
                if norm == c_norm
                or c_norm in norm
                or norm in c_norm
                or _tokens_in_order(tokens, c_tokens)
            ]
            if len(matches) != 1:
                logger.warning(
                    "poi id %r unresolvable (candidates=%s); table will fall back",
                    pid, matches if len(matches) <= 3 else f"{len(matches)} matches",
                )
                return None
            canon = matches[0]
            notes.append(f"{pid!r}->{canon}")
            fixed.append(canon)
        deduped = list(dict.fromkeys(fixed))
        if len(deduped) != len(fixed):
            notes.append("dropped same-day duplicate id")
        entry["poi_ids"] = deduped
    return notes


def _repair_poi_set(
    groups_by_local_day: dict[int, list[Poi]],
    by_id: dict[str, Poi],
    empty_ok: set[int],
) -> list[str] | None:
    """Fix a minor ``poi set mismatch`` instead of dropping the whole table.

    The model sometimes omits one selected spot or lists the same spot on two
    days (prod 2026-08-28: 10-spot tokyo table, "poi set mismatch"). Omissions
    are appended to the nearest non-empty day (never an arrival/departure
    ``none`` day); cross-day duplicates keep the occurrence closest to their
    day's centroid. ``None`` = beyond minor repair (>2 discrepancies) —
    caller falls back to rule-based. Returns a human-readable move list."""
    missing = [pid for pid in by_id if pid not in set(groups_by_local_day and
                 [p.id for g in groups_by_local_day.values() for p in g] or [])]
    seen: list[str] = [p.id for g in groups_by_local_day.values() for p in g]
    dupes = sorted({pid for pid in seen if seen.count(pid) > 1})
    if not missing and not dupes:
        return []
    if len(missing) + len(dupes) > 2:
        return None
    notes: list[str] = []
    for pid in dupes:
        occurrences = [(d, g) for d, g in groups_by_local_day.items() if any(p.id == pid for p in g)]
        if len(occurrences) < 2:
            continue  # same-day duplicate already rejected per-entry upstream
        centroids = {
            d: _centroid([p for p in groups_by_local_day[d] if p.id != pid] or
                         [p for p in groups_by_local_day[d]])
            for d, _ in occurrences
        }
        poi = next(p for p in groups_by_local_day[occurrences[0][0]] if p.id == pid)
        keep_day = min(occurrences, key=lambda t: haversine_km(poi.lat, poi.lng, *centroids[t[0]]))[0]
        for d, _ in occurrences:
            if d != keep_day:
                groups_by_local_day[d] = [p for p in groups_by_local_day[d] if p.id != pid]
                notes.append(f"dropped duplicate {pid} on day {d}")
    for pid in missing:
        poi = by_id[pid]
        # T-052: anchor-follow — a missing spot joins the day holding the
        # geographically closest already-placed spot (≤ 8 km), so far-suburb
        # siblings (grand park + skylift) cluster together instead of being
        # scattered by centroid arithmetic.
        anchor = None
        for day, group in sorted(groups_by_local_day.items()):
            if day in empty_ok or not group:
                continue
            for q in group:
                d = haversine_km(poi.lat, poi.lng, q.lat, q.lng)
                if d <= _REPAIR_ANCHOR_KM and (anchor is None or d < anchor[0]):
                    anchor = (d, day, q)
        if anchor is not None:
            _, day, q = anchor
            day_list = groups_by_local_day[day]
            day_list.insert(day_list.index(q) + 1, poi)
            notes.append(f"added missing {pid} next to {q.id} on day {day}")
            continue
        candidates = [
            d for d, g in groups_by_local_day.items() if g and d not in empty_ok
        ] or [d for d, g in groups_by_local_day.items() if g]
        if not candidates:
            return None
        dest = min(candidates, key=lambda d: haversine_km(poi.lat, poi.lng, *_centroid(groups_by_local_day[d])))
        _insert_best_position(groups_by_local_day[dest], poi)
        notes.append(f"added missing {pid} to day {dest}")
    # Re-verify the invariant actually holds now.
    seen = [p.id for g in groups_by_local_day.values() for p in g]
    if set(seen) != set(by_id) or len(seen) != len(by_id):
        return None
    return notes


def _repair_districts(
    days: list[list[Poi]],
) -> tuple[list[list[Poi]], list[tuple[str, int, int]]]:
    """Surgically fix a district-split instead of throwing the table away
    (2026-08-28: ueno-park grouped with two far-west suburbs killed an
    otherwise-good 4/5 of a table, and whole-city fallback was worse than the
    mistake).

    A spot that is > ``DISTRICT_OUTLIER_KM`` from its own day's centroid while
    some other non-empty day is > ``DISTRICT_CLOSER_OTHER_KM`` closer moves to
    that day (at the chain position adding least distance). Days keep ≥1 spot:
    single-spot days can't produce outliers, and moving never empties a source
    day. Caller still re-runs :func:`_districts_ok` — repair is a second
    chance, not a licence.
    """
    days = [list(group) for group in days]
    moved: list[tuple[str, int, int]] = []
    for _ in range(2):  # a move shifts centroids; re-scan after each
        centroids = [_centroid(g) if g else None for g in days]
        violation: tuple[int, Poi, int, float] | None = None
        for i, group in enumerate(days):
            if len(group) < 2 or centroids[i] is None:
                continue
            for poi in group:
                dist_own = haversine_km(poi.lat, poi.lng, *centroids[i])
                if dist_own <= DISTRICT_OUTLIER_KM:
                    continue
                dests = [
                    (haversine_km(poi.lat, poi.lng, *centroids[j]), j)
                    for j in range(len(days))
                    if j != i and days[j]
                ]
                if not dests:
                    continue
                dist_best, j_best = min(dests)
                if dist_own - dist_best > DISTRICT_CLOSER_OTHER_KM:
                    violation = (i, poi, j_best, dist_best)
                    break
            if violation:
                break
        if violation is None:
            return days, moved
        if len(moved) >= 4:  # runaway scatter means the table is beyond repair
            return days, moved
        i, poi, j, _dist = violation
        days[i].remove(poi)
        _insert_best_position(days[j], poi)
        moved.append((poi.id, i + 1, j + 1))
    return days, moved


def _even_split(n_slots: int, n_items: int) -> list[int]:
    if n_slots <= 0:
        return []
    base, extra = divmod(n_items, n_slots)
    return [base + (1 if i < extra else 0) for i in range(n_slots)]


def _edge_target_counts(
    n_days: int,
    n_pois: int,
    first_mode: str | None,
    last_mode: str | None,
) -> list[int]:
    """How many POIs each local day should get.

    none = 0 when some other day in this city can take the spots.
    few = prefer ``IDEAL_FEW_POIS`` (1), but take more if middle days would
    otherwise go past ``REASONABLE_DAY_POIS``, or if this city has no middle
    day (e.g. a 2-day trip with 4–5 spots — few is not a hard 1).
    A 1-day city block is left untouched: emptying it would drop the city.
    """
    if n_days <= 1:
        return [n_pois]
    modes: list[str | None] = [None] * n_days
    if first_mode is not None:
        modes[0] = first_mode
    if last_mode is not None:
        modes[-1] = last_mode
    none_idx = [i for i, mode in enumerate(modes) if mode == "none"]
    few_idx = [i for i, mode in enumerate(modes) if mode == "few"]
    mid_idx = [i for i, mode in enumerate(modes) if mode is None]
    if not few_idx and not mid_idx:
        return _even_split(n_days, n_pois)

    counts = [0] * n_days
    leftover = n_pois
    if mid_idx:
        for i in few_idx:
            take = min(IDEAL_FEW_POIS, leftover)
            counts[i] = take
            leftover -= take
        mid_counts = _even_split(len(mid_idx), leftover)
        overflow = 0
        capped: list[int] = []
        for count in mid_counts:
            if count > REASONABLE_DAY_POIS:
                overflow += count - REASONABLE_DAY_POIS
                capped.append(REASONABLE_DAY_POIS)
            else:
                capped.append(count)
        # T-048: overflow no longer pours into the few days — once the packer
        # grew a budget guard, arrival days physically cannot absorb it (the
        # old logic set an arrival-day target of 8 spots for 17 spots / 6 days).
        # Saturated is saturated: middle days split the overload, and packing +
        # the closing-time final check handle it honestly.
        if overflow:
            extra = _even_split(len(mid_idx), overflow)
            capped = [c + a for c, a in zip(capped, extra)]
        for i, count in zip(mid_idx, capped):
            counts[i] = count
    else:
        split = _even_split(len(few_idx), leftover)
        for i, count in zip(few_idx, split):
            counts[i] = count
    return counts


def _rebalance_edge_days(
    groups: list[list[Poi]],
    *,
    first_mode: str | None,
    last_mode: str | None,
) -> list[list[Poi]]:
    """Trim the trip's global first/last day (when they land in this city).

    Does not re-run geographic clustering: POIs stay in their current
    concatenated order, only the day cuts move.
    """
    if first_mode is None and last_mode is None:
        return groups
    n = len(groups)
    n_pois = sum(len(group) for group in groups)
    targets = _edge_target_counts(n, n_pois, first_mode, last_mode)
    flat = [poi for group in groups for poi in group]
    out: list[list[Poi]] = []
    cursor = 0
    for want in targets:
        out.append(flat[cursor:cursor + want])
        cursor += want
    return out


def _is_full_day(poi: Poi) -> bool:
    return poi.suggested_duration_min >= FULL_DAY_DURATION_MIN


def _two_opt_order(day: list[Poi]) -> list[Poi]:
    """Deterministic 2-opt on the day's spot sequence (T-039). Hotel endpoints
    are excluded — districts are small enough that interior ordering is where
    the waste is. No-op for <4 spots."""
    if len(day) < 4:
        return day

    def length(seq: list[Poi]) -> float:
        return sum(haversine_km(a.lat, a.lng, b.lat, b.lng) for a, b in zip(seq, seq[1:]))

    best = list(day)
    best_len = length(best)
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 2, len(best)):
                cand = best[:i + 1] + best[i + 1:j + 1][::-1] + best[j + 1:]
                cand_len = length(cand)
                if cand_len + 1e-9 < best_len:
                    best, best_len = cand, cand_len
                    improved = True
    return best


# A spot closing at/before this hour is treated as time-boxed (morning
# markets, early museums) and goes to the front of its day (T-036).
EARLY_CLOSING_MIN = 15 * 60


def _close_time(poi: Poi) -> int:
    return parse_hours(poi.opening_hours)[1] or 24 * 60


def _order_day(day: list[Poi]) -> list[Poi]:
    """Final within-day order: 2-opt for geography (T-039), then early-closing
    spots move to the front, earliest first (T-036) — the rest keep their
    relative order. Runs after all packing/repair, right before chain build."""
    ordered = _two_opt_order(day)
    early = sorted(
        (p for p in ordered if _close_time(p) <= EARLY_CLOSING_MIN), key=_close_time,
    )
    if not early or len(early) == len(ordered):
        return ordered
    early_ids = {p.id for p in early}
    return early + [p for p in ordered if p.id not in early_ids]


def _hop_min(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> int:
    """T-045b: minutes for one intra-city hop — "15 + 3.5 min/km", fitted on
    163 real SerpApi cache entries (2 km ~= 20, 5 km ~= 33, 8 km ~= 43;
    between median and P90), replacing the flat 25 min (real Tokyo median for
    a 6-10 km hop is 41 min, so the old value badly under-measured)."""
    km = haversine_km(a_lat, a_lng, b_lat, b_lng)
    return min(int(_HOP_BASE_MIN + _HOP_PER_KM_MIN * km), 90)


_HOP_BASE_MIN = 15
_HOP_PER_KM_MIN = 3.5


def _closed_at_arrival(
    order: list[Poi],
    start_min: int,
    first_from: tuple[float, float] | None = None,
    end_min: int | None = None,
) -> list[int]:
    """Indices the day clock reaches AFTER the spot became unvisitable
    (planner approximation of schedule.py's honest clipping:
    distance-calibrated hops, lunch 60 once the clock crosses noon).
    ``first_from`` is the day's morning-hotel coordinate — the hop to the
    FIRST spot is real transit too (2026-08-28 prod: skipping that hop let
    ueno pass as a phantom 17:27 arrival; real arrival 18:22). ``end_min`` is
    the day's hard cap
    (22:00, or the departure-day flight cutoff) — a 24h spot can still be
    unreachable when the day is simply too full (harajuku 2026-08-28).
    Unknown closing time is treated as open — only a *parsed* close can
    veto."""
    clock = start_min
    lunch_done = False
    closed: list[int] = []
    prev: tuple[float, float] | None = first_from
    for k, poi in enumerate(order):
        if prev is not None:
            clock += _hop_min(prev[0], prev[1], poi.lat, poi.lng)
        opens, closes = parse_hours(poi.opening_hours)
        if opens is not None and clock < opens:
            clock = opens
        if closes is not None and clock >= closes:
            closed.append(k)
            continue
        if end_min is not None and clock >= end_min:
            closed.append(k)
            continue
        stay = poi.suggested_duration_min or 90
        if closes is not None:
            stay = min(stay, closes - clock)
        if end_min is not None:
            stay = min(stay, end_min - clock)  # matches schedule: visit clipped by the day cap
        clock_before = clock
        clock += max(stay, 0)
        # T-045b fix: insert lunch only when the visit truly straddles the
        # 11:30-14:00 window — the old "past noon = +60" rule charged lunch to
        # an 8 pm visit too, inflating evening days by 1 hour (24h shinjuku was
        # falsely flagged as over the line, confirmed 2026-08-29).
        if not lunch_done and clock_before < 14 * 60 and clock >= 12 * 60:
            clock += LUNCH_MIN
            lunch_done = True
        prev = (poi.lat, poi.lng)
    return closed


def _reorder_for_closing(
    order: list[Poi],
    start_min: int,
    first_from: tuple[float, float] | None = None,
    end_min: int | None = None,
) -> list[Poi]:
    """T-045 final check: spots over the line (closing time or day cap) move
    earlier within their day until everything fits or is confirmed
    unrescuable. If nothing can move, keep the original order (the unvisited
    warning is still reported honestly)."""
    order = list(order)
    if len(order) < 2:
        return order
    for _ in range(len(order)):
        bad = _closed_at_arrival(order, start_min, first_from, end_min)
        if not bad:
            return order
        k = bad[0]
        spot = order.pop(k)
        placed = False
        for pos in range(k):  # earlier positions only; moving it later never helps
            trial = order[:pos] + [spot] + order[pos:]
            if not _closed_at_arrival(trial, start_min, first_from, end_min):
                order = trial
                placed = True
                break
        if not placed:
            order.insert(k, spot)
            break
    return order


def _enforce_arrival_proximity(
    groups: list[list[Poi]],
    starts: dict[int, int],
    ends: dict[int, int],
    first_from_by_day: dict[int, tuple[float, float] | None] | None,
    hotel: tuple[float, float],
) -> tuple[list[list[Poi]], list[tuple[str, int, int]]]:
    """T-051: arrival-day geographic anchoring (only the block owning day 1).

    After landing + check-in + a long flight, dragging the traveller to a
    district 8km away and back was the harshest review this product got. Rules:
    1. On the arrival day (local day 0), spots farther than
       ``_ARRIVAL_PROXIMITY_KM`` from the hotel move to the geographically
       closest day where they still clear the closing-time line;
    2. If that empties the arrival day, pull in the nearest (<=5km) spot from
       another day ("few" means one nearby spot when there is room).
    Moves stay within the city block; cross-city is trip structure, untouchable."""
    groups = [list(g) for g in groups]
    moved: list[tuple[str, int, int]] = []
    i = 0  # the arrival day is always this block's first day

    def start_of(j: int) -> int:
        return starts.get(j, DAY_START_MIN)

    def end_of(j: int) -> int | None:
        return (ends or {}).get(j)

    def origin_of(j: int) -> tuple[float, float] | None:
        return (first_from_by_day or {}).get(j)

    def km_from_hotel(p: Poi) -> float:
        return haversine_km(hotel[0], hotel[1], p.lat, p.lng)

    # 1) exile far spots: move the farthest first until none exceed 5km
    for _ in range(len(groups[i]) + 1):
        far = [
            (km_from_hotel(p), k)
            for k, p in enumerate(groups[i])
            if km_from_hotel(p) > _ARRIVAL_PROXIMITY_KM
        ]
        if not far:
            break
        _, k = max(far)
        spot = groups[i].pop(k)
        dests: list[tuple[float, int, list[Poi]]] = []
        for j in range(len(groups)):
            if j == i or not groups[j]:
                continue
            trial = _reorder_for_closing(
                groups[j] + [spot], start_of(j), origin_of(j), end_of(j)
            )
            if _closed_at_arrival(trial, start_of(j), origin_of(j), end_of(j)):
                continue  # inserting would breach that day's closing line - skip
            c = _centroid(groups[j] or [spot])
            dist = haversine_km(spot.lat, spot.lng, c[0], c[1])
            dests.append((dist, j, trial))
        if not dests:
            groups[i].insert(k, spot)  # nowhere legal -> keep in place; upstream reports
            break
        _, j, trial = min(dests)
        groups[j] = trial
        moved.append((spot.id, i + 1, j + 1))

    # 2) arrival day emptied -> pull in the nearest (<=5km) spot from another day
    if not groups[i]:
        cands: list[tuple[float, int, Poi]] = []
        for j in range(len(groups)):
            if j == i:
                continue
            for p in groups[j]:
                d = km_from_hotel(p)
                if d <= _ARRIVAL_PROXIMITY_KM:
                    cands.append((d, j, p))
        if cands:
            _, j, p = min(cands)
            groups[j] = [q for q in groups[j] if q.id != p.id]
            groups[i] = [p]
            moved.append((p.id, j + 1, i + 1))
    return groups, moved


def _enforce_closing(
    groups: list[list[Poi]],
    starts: dict[int, int],
    empty_idx: set[int] | frozenset[int],
    budget_by_idx: list[int] | None,
    first_from_by_day: dict[int, tuple[float, float] | None] | None = None,
    ends_by_day: dict[int, int] | None = None,
) -> tuple[list[list[Poi]], list[tuple[str, int, int]], list[str]]:
    """T-045 final check main entry: keep "unreachable" spots alive.

    1. Reorder within the day (:func:`_reorder_for_closing`) — early-closing
       / over-the-cap spots move to the front;
    2. Spots still over the line move across days: the target day must have
       room (budget), the whole-day simulation must stay feasible after the
       insertion, and it must not be an arrival/departure day kept empty;
    3. If even a cross-day move cannot save it -> the ids go back to the
       caller as warnings (the last line of honest reporting).
    Only spots move, never hotels (hotels are settled before this stage;
    "nearest hotel after a day swap" yields to correctness).
    ``first_from_by_day`` is each day's morning-hotel coordinate — the first
    hop (hotel -> first spot) is real transit and cannot count as 0 min.
    ``ends_by_day`` is each day's hard cap (22:00, or the departure-day
    flight cutoff) — even a 24h spot gets squeezed out by an over-packed day
    (harajuku, hit in prod 2026-08-28)."""
    groups = [list(g) for g in groups]
    moved: list[tuple[str, int, int]] = []

    def start_of(i: int) -> int:
        return starts.get(i, DAY_START_MIN)

    def end_of(i: int) -> int | None:
        return (ends_by_day or {}).get(i)

    def origin_of(i: int) -> tuple[float, float] | None:
        return (first_from_by_day or {}).get(i)

    def budget_of(i: int) -> int:
        return budget_by_idx[i] if budget_by_idx and i < len(budget_by_idx) else DAYLIGHT_BUDGET_MIN

    for i in range(len(groups)):
        order = _reorder_for_closing(list(groups[i]), start_of(i), origin_of(i), end_of(i))
        for _ in range(len(order) + 2):
            order = _reorder_for_closing(order, start_of(i), origin_of(i), end_of(i))
            bad = _closed_at_arrival(order, start_of(i), origin_of(i), end_of(i))
            if not bad:
                break
            spot = order[bad[-1]]
            # — Rescue 1: move across days (target day stays closing-feasible) —
            # T-053: budget guard removed - a closing violation rescue takes
            # priority over capacity management; a slightly crowded target day
            # with a saved spot beats a warning left on the original day.
            dests: list[tuple[float, int, int, list[Poi]]] = []
            for j in range(len(groups)):
                if j == i or j in empty_idx or not groups[j]:
                    continue
                trial = _reorder_for_closing(groups[j] + [spot], start_of(j), origin_of(j), end_of(j))
                if not _closed_at_arrival(trial, start_of(j), origin_of(j), end_of(j)):
                    # T-054: nearest feasible day, not lightest — a closing
                    # rescue that hops the city (Arashiyama -> the far east)
                    # trades one warning for a worse itinerary
                    dests.append(_day_geo_key(groups[j], spot) + (j, trial))
            if dests:
                _, _, j, trial = min(dests)
                groups[j] = trial
                order.pop(bad[-1])
                moved.append((spot.id, i + 1, j + 1))
                continue
            # — Rescue 2: swap across days — trade for a spot from another
            # day that still fits this evening; budget guards removed (T-053):
            # closing-feasibility is the only hard constraint.
            swapped = False
            geo_order = sorted(
                (j for j in range(len(groups)) if j != i and j not in empty_idx and groups[j]),
                key=lambda j: _day_geo_key(groups[j], spot),
            )
            for j in geo_order:
                for s in list(groups[j]):
                    if s.id == spot.id:
                        continue
                    trial_i = _reorder_for_closing(
                        [p for p in groups[i] if p.id != spot.id] + [s], start_of(i), origin_of(i), end_of(i),
                    )
                    if _closed_at_arrival(trial_i, start_of(i), origin_of(i), end_of(i)):
                        continue
                    trial_j = _reorder_for_closing(
                        [p for p in groups[j] if p.id != s.id] + [spot], start_of(j), origin_of(j), end_of(j),
                    )
                    if _closed_at_arrival(trial_j, start_of(j), origin_of(j), end_of(j)):
                        continue
                    groups[i], groups[j] = trial_i, trial_j
                    order = trial_i
                    moved.append((f"{spot.id}<->{s.id}", i + 1, j + 1))
                    swapped = True
                    break
                if swapped:
                    break
            if not swapped:
                # — Rescue 3 (last resort): move into a none (empty) day —
                # "none" means "as light as possible", not "forbidden": when no
                # regular day fits and an empty day can hold the spot without
                # missing the line, using the empty day beats dropping a spot
                # the user selected (2026-08-29 prod: kinkaku-ji on an
                # intercity day had nowhere to go).
                empty_dests: list[tuple[float, int, int, list[Poi]]] = []
                for j in sorted(empty_idx):
                    if j == i:
                        continue
                    trial = _reorder_for_closing(groups[j] + [spot], start_of(j), origin_of(j), end_of(j))
                    if not _closed_at_arrival(trial, start_of(j), origin_of(j), end_of(j)):
                        empty_dests.append(_day_geo_key(groups[j], spot) + (j, trial))
                if empty_dests:
                    _, _, j, trial = min(empty_dests)
                    groups[j] = trial
                    order.pop(bad[-1])
                    moved.append((spot.id, i + 1, j + 1))
                    continue
                break
        groups[i] = order

    remaining: list[str] = []
    for i, g in enumerate(groups):
        for k in _closed_at_arrival(g, start_of(i), origin_of(i), end_of(i)):
            remaining.append(g[k].id)
    return groups, moved, sorted(set(remaining))


# Minimum open-in-window overlap for a POI to be considered visitable (T-034).
FEASIBLE_OVERLAP_MIN = 30
# A window's start is optimistic — the traveller still has to check in and
# ride transit before the first spot. Shave this off when testing whether a
# spot can actually be reached while open.
WINDOW_MARGIN_MIN = 60


def _window_feasible(poi: Poi, start_min: int, end_min: int) -> bool:
    """Can ``poi`` actually be visited inside ``[start_min, end_min]``?

    Unknown hours are assumed feasible — we only veto when the catalog says
    the place cannot possibly be open long enough in the day's window. The
    start gets ``WINDOW_MARGIN_MIN`` shaved off (check-in + transit).
    """
    opens, closes = parse_hours(poi.opening_hours)
    lo = start_min + WINDOW_MARGIN_MIN if opens is None else max(start_min + WINDOW_MARGIN_MIN, opens)
    hi = end_min if closes is None else min(end_min, closes)
    return hi - lo >= FEASIBLE_OVERLAP_MIN


def _move_infeasible_for_windows(
    groups: list[list[Poi]],
    windows: dict[int, tuple[int, int]],
    empty_idx: set[int] | frozenset[int],
) -> tuple[list[list[Poi]], list[tuple[str, int, int]], list[str]]:
    """T-034: a spot that can never be open during its day's reachable
    window (late flight landing, mid-afternoon intercity arrival, early
    departure cutoff) is moved to the lightest day where it IS visitable.
    Days that must stay empty (``none``) never receive moves. Spots with no
    legal home stay put and are reported so the trip response can warn."""
    if not windows:
        return groups, [], []
    packed = [list(group) for group in groups]
    moved: list[tuple[str, int, int]] = []
    stuck: list[str] = []
    for idx in sorted(windows):
        start, end = windows[idx]
        keep: list[Poi] = []
        exile: list[Poi] = []
        for poi in packed[idx]:
            (exile if not _window_feasible(poi, start, end) else keep).append(poi)
        if not exile:
            continue
        packed[idx] = keep
        for poi in exile:
            dests = [j for j in range(len(packed)) if j != idx and j not in empty_idx]
            if not dests:
                packed[idx].append(poi)
                stuck.append(poi.id)
                continue
            # T-054: geography first here too (see _pack_day_capacity)
            dest = min(dests, key=lambda j: _day_geo_key(packed[j], poi))
            packed[dest].append(poi)
            moved.append((poi.id, idx, dest))
    return packed, moved, stuck


def _isolate_full_day_pois(groups: list[list[Poi]]) -> list[list[Poi]]:
    """Disney / USJ / similar: one park, that whole day, nothing else.

    Runs after edge rebalance so a ``none`` last day stays empty. Displaced
    neighbours go to other non-park days, never onto the park day.
    """
    n = len(groups)
    parks = [poi for group in groups for poi in group if _is_full_day(poi)]
    if not parks:
        return groups
    new = [[poi for poi in group if not _is_full_day(poi)] for group in groups]
    originally_empty = {i for i, group in enumerate(groups) if not group}
    used: set[int] = set()

    def pick_day() -> int:
        middles = [i for i in range(1, max(1, n - 1)) if i not in used]
        if middles:
            return middles[0]
        for i in range(n):
            if i not in used and i not in originally_empty:
                return i
        for i in range(n):
            if i not in used:
                return i
        return 0

    for park in parks:
        idx = pick_day()
        displaced = new[idx]
        new[idx] = [park]
        used.add(idx)
        sinks = [i for i in range(n) if i not in used and i not in originally_empty]
        if not sinks:
            sinks = [i for i in range(n) if i not in used]
        for poi in displaced:
            if not sinks:
                new[idx].append(poi)
                continue
            sink = min(sinks, key=lambda i: len(new[i]))
            new[sink].append(poi)
    return new


def _select_hotels_system_one(city_lodgings: list[Lodging], day_groups: list[list[Poi]]) -> Lodging:
    # T-016: an edge day trimmed to 0 pois (edge_density="...none...")
    # contributes no anchor point — score against whichever days still have
    # pois; if none do (shouldn't happen, F guarantees >=1 poi selected
    # somewhere in the city) just take the first candidate.
    centroids = [_centroid(group) for group in day_groups if group]
    if not centroids:
        return city_lodgings[0]

    def score(lodging: Lodging) -> float:
        return sum(haversine_km(lodging.lat, lodging.lng, clat, clng) for clat, clng in centroids)

    return min(city_lodgings, key=score)


def _select_hotels_system_multi(
    city_lodgings: list[Lodging], day_numbers: list[int], day_groups: list[list[Poi]]
) -> dict[int, Lodging]:
    result: dict[int, Lodging] = {}
    previous: Lodging | None = None
    non_empty_pois = [p for group in day_groups for p in group]
    for day, group in zip(day_numbers, day_groups):
        if not group:
            # T-016: edge day with no pois — nothing to optimize against,
            # so just carry the running hotel forward (or, if this is the
            # very first day, seed from this city's overall poi centroid).
            if previous is not None:
                chosen = previous
            elif non_empty_pois:
                clat, clng = _centroid(non_empty_pois)
                chosen = _nearest_lodging(clat, clng, city_lodgings)
            else:
                chosen = city_lodgings[0]
            result[day] = chosen
            previous = chosen
            continue
        last_poi = group[-1]
        candidate = _nearest_lodging(last_poi.lat, last_poi.lng, city_lodgings)
        if previous is not None and haversine_km(last_poi.lat, last_poi.lng, previous.lat, previous.lng) < HOTEL_STICKINESS_KM:
            chosen = previous
        else:
            chosen = candidate
        result[day] = chosen
        previous = chosen
    return result


def _hours_hint(poi: Poi) -> str:
    hours = poi.opening_hours or "hours unknown"
    opens, closes = parse_hours(poi.opening_hours)
    if opens == 0 and closes == 24 * 60:
        return f"{hours}; open all day"
    if closes is not None:
        return f"{hours}; finish by {closes // 60:02d}:{closes % 60:02d}"
    return hours


def _deepseek_city_prompt(
    city: str,
    pois: list[Poi],
    n_days: int,
    hotel_mode: str,
    candidates: list[Lodging],
    required_lodging_by_local_day: dict[int, Lodging] | None,
    first_mode: str | None = None,
    last_mode: str | None = None,
    arrival_start_min: int | None = None,
    departure_cutoff_min: int | None = None,
    arrival_hub: TransportHub | None = None,
    departure_hub: TransportHub | None = None,
) -> str:
    poi_lines = "\n".join(
        f"- {p.id}: {p.name_en} ({p.lat:.4f},{p.lng:.4f}, "
        f"{p.suggested_duration_min} min, {_hours_hint(p)})"
        for p in pois
    )
    if required_lodging_by_local_day:
        lodging_block = "Lodging is fixed by the traveller for each day (hotel_mode=custom):\n" + "\n".join(
            f"- day {d}: {l.id} ({l.lat:.4f},{l.lng:.4f})"
            for d, l in sorted(required_lodging_by_local_day.items())
        )
    else:
        cand_lines = "\n".join(f"- {l.id}: {l.name} ({l.lat:.4f},{l.lng:.4f})" for l in candidates)
        if hotel_mode == "system_one":
            lodging_block = (
                "Pick exactly ONE lodging id from this list and use it for every single day "
                f"(hotel_mode=system_one, one hotel for the whole city block):\n{cand_lines}"
            )
        else:
            lodging_block = (
                "Pick a lodging id from this list for each day, closest to that day's last "
                f"stop; switching hotels between days is fine (hotel_mode=system_multi):\n{cand_lines}"
            )
    edge_rules: list[str] = [
        "Keep geographically close spots on the SAME day. Do not even out the number of spots per day.",
        "A spot whose duration is 360 minutes or more must be the only spot that day (theme parks).",
        (
            "Fit each day in about 9 hours: sum of stay minutes + 60 min lunch + 90 min dinner "
            "+ 25 min per hop between spots, and never past 22:00. If a day overflows, move "
            "the last non-park spot to another day that is not an empty arrival/departure day."
        ),
        (
            "Visit only while open. Use each spot's hours. Museums/parks must finish before they close. "
            "Do not schedule a stop that would start after closing."
        ),
        (
            "If a spot closes unusually early (for example a morning market that shuts after lunch), "
            "make it the FIRST stop of its day."
        ),
        "Do not put dinner or extra sightseeing after the departure airport/station.",
    ]
    if first_mode is not None and arrival_start_min is not None:
        lands = _hhmm(arrival_start_min - LANDING_BUFFER_MIN)
        edge_rules.append(
            f"Day 1: the flight lands at {lands}; after immigration, baggage and getting into "
            f"the city, sightseeing cannot start before {_hhmm(arrival_start_min)}. Keep day 1 "
            f"within {DAY_END_MIN - arrival_start_min} minutes of total activity — usually just "
            "1 nearby spot plus check-in. That evening the day's LAST spot must still be open "
            "at the hour the traveller reaches it — put early-closing places (museums) first "
            "and late-opening areas last. And every arrival-day spot must sit within about "
            "5 km of that night's hotel — never cross the city on arrival evening; leave far "
            "districts for full days."
        )
    if last_mode is not None and departure_cutoff_min is not None:
        departs = _hhmm(departure_cutoff_min + TAKEOFF_BUFFER_MIN)
        edge_rules.append(
            f"Day {n_days}: the flight departs at {departs}; the traveller must be at the "
            f"airport by {_hhmm(departure_cutoff_min)}, so every stop that day must FINISH by "
            f"{_hhmm(departure_cutoff_min)} — check each spot's stay time against this deadline."
        )
    # T-042: edge days should anchor on what the traveller actually needs
    # (drop bags first / be near the way out), not on model preference.
    if first_mode is not None and arrival_hub is not None:
        edge_rules.append(
            f"Day 1 is the arrival day: the traveller lands at {arrival_hub.name_en or arrival_hub.name} "
            "and checks in first — pick the arrival-day spot closest to that night's hotel."
        )
    if last_mode is not None and departure_hub is not None:
        edge_rules.append(
            f"Day {n_days} ends at {departure_hub.name_en or departure_hub.name} "
            f"({departure_hub.lat:.4f},{departure_hub.lng:.4f}) — pick that day's spot(s) "
            "as close to it as possible."
        )
    both_none_two_days = first_mode == "none" and last_mode == "none" and n_days == 2
    if first_mode == "none" and not both_none_two_days:
        edge_rules.append("Day 1 is the trip arrival day: poi_ids must be [].")
    elif first_mode == "few":
        edge_rules.append(
            "Day 1 is the trip arrival day: keep it light (prefer 1 spot; more only if later days cannot hold the rest)."
        )
    if last_mode == "none" and not both_none_two_days:
        edge_rules.append(f"Day {n_days} is the trip departure day: poi_ids must be [].")
    elif last_mode == "few":
        edge_rules.append(
            f"Day {n_days} is the trip departure day: keep it light (prefer 1 spot; more only if earlier days cannot hold the rest)."
        )
    if both_none_two_days:
        edge_rules.append(
            "Both arrival and departure are none but there are only 2 days — spots cannot be dropped, so split them by area across both days (neither day empty)."
        )
    rules = "\n".join(f"- {r}" for r in edge_rules)
    return (
        f"You are planning {n_days} day(s) in {city} for a budget backpacker. Group the "
        "spots below into day-by-day itineraries so each day covers spots that are "
        "geographically close and in a sensible walking/transit order. Every spot must be "
        f"used exactly once across all {n_days} day(s) — none skipped, none repeated. Also "
        "assign a lodging id to each day.\n\n"
        f"Rules:\n{rules}\n\n"
        f"Spots:\n{poi_lines}\n\n{lodging_block}\n\n"
        "Reply with ONLY a JSON object of the exact form "
        '{"days": [{"day": 1, "city": "' + city + '", "poi_ids": ["..."], '
        '"lodging_id": "..."}, ...]}. '
        "Copy poi_ids and lodging_id values character-for-character from the lists above — "
        "do not abbreviate, reword or re-space them. "
        f"``day`` must run 1..{n_days}, each exactly once."
    )


def _deepseek_fill_city(
    *,
    city: str,
    pois: list[Poi],
    n_days: int,
    hotel_mode: str,
    candidates: list[Lodging],
    required_lodging_by_local_day: dict[int, Lodging] | None,
    api_key: str,
    base_url: str,
    first_mode: str | None = None,
    last_mode: str | None = None,
    arrival_start_min: int | None = None,
    departure_cutoff_min: int | None = None,
    arrival_hub: TransportHub | None = None,
    departure_hub: TransportHub | None = None,
) -> tuple[list[list[Poi]], dict[int, Lodging]] | None:
    """One DeepSeek call for one city. Returns ``None`` on *any* problem
    (HTTP failure, malformed JSON, or a validation miss against 6.1b F) so
    the caller can fall back to the rule-based v1 algorithm for just this
    city — never a 500, never a retry loop. Fallback is a failure, not the
    happy path.
    """
    if not pois:
        return None
    prompt = _deepseek_city_prompt(
        city, pois, n_days, hotel_mode, candidates, required_lodging_by_local_day,
        first_mode=first_mode, last_mode=last_mode,
        arrival_start_min=arrival_start_min, departure_cutoff_min=departure_cutoff_min,
        arrival_hub=arrival_hub, departure_hub=departure_hub,
    )
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=deepseek_messages_body(prompt),
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        logger.info(
            "DeepSeek fill city=%s stop=%s blocks=%s usage=%s",
            city,
            payload.get("stop_reason"),
            _content_block_types(payload.get("content")),
            payload.get("usage"),
        )
        text = _find_text_block(payload["content"])
        try:
            parsed = _extract_json(text)
        except Exception as parse_exc:
            # Without the raw reply there is no fixing the prompt/parser: log
            # its head on failure
            logger.error(
                "DeepSeek reply unparseable for city=%s (%s); head=%.300r",
                city, parse_exc, text[:300],
            )
            return None
    except Exception as exc:  # noqa: BLE001 — per-city fallback is a requirement
        logger.error("DeepSeek fill FAILED for city=%s (%s); falling back to rule-based", city, exc)
        return None

    raw_days = parsed.get("days")
    if not isinstance(raw_days, list) or len(raw_days) != n_days:
        logger.error("DeepSeek reply for city=%s has wrong day count; falling back", city)
        return None

    by_id = {p.id: p for p in pois}
    lodging_by_id = {l.id: l for l in candidates}
    if required_lodging_by_local_day:
        lodging_by_id.update({l.id: l for l in required_lodging_by_local_day.values()})

    # A mis-echoed id used to kill the whole model table (2026-08-28 prod,
    # 17-spot tokyo: "unknown or duplicate poi id" ×2). Repair near-misses
    # first; only an unresolvable id still falls back to rule-based.
    id_repairs = _repair_poi_ids(raw_days, by_id)
    if id_repairs is None:
        logger.error("DeepSeek fill FAILED for city=%s (unresolvable poi id); falling back", city)
        return None
    if id_repairs:
        logger.info("DeepSeek poi-id repair city=%s %s", city, id_repairs)

    both_none_two_days = first_mode == "none" and last_mode == "none" and n_days == 2
    empty_ok: set[int] = set()
    if first_mode == "none" and not both_none_two_days:
        empty_ok.add(1)
    if last_mode == "none" and not both_none_two_days:
        empty_ok.add(n_days)

    groups_by_local_day: dict[int, list[Poi]] = {}
    lodging_by_local_day: dict[int, Lodging] = {}
    seen_ids: list[str] = []
    for entry in raw_days:
        if not isinstance(entry, dict):
            logger.error("DeepSeek fill FAILED for city=%s (day entry not an object); falling back", city)
            return None
        day = entry.get("day")
        poi_ids = entry.get("poi_ids")
        lodging_id = entry.get("lodging_id")
        if not isinstance(day, int) or day in groups_by_local_day:
            logger.error("DeepSeek fill FAILED for city=%s (bad/duplicate day); falling back", city)
            return None
        if not isinstance(poi_ids, list):
            logger.error("DeepSeek fill FAILED for city=%s (poi_ids not a list); falling back", city)
            return None
        if not poi_ids and day not in empty_ok:
            logger.error("DeepSeek fill FAILED for city=%s (empty day %s not allowed); falling back", city, day)
            return None
        resolved = [by_id.get(pid) for pid in poi_ids]
        if any(p is None for p in resolved) or len(set(poi_ids)) != len(poi_ids):
            bad = [pid for pid, p in zip(poi_ids, resolved) if p is None]
            logger.error(
                "DeepSeek fill FAILED for city=%s (unknown or duplicate poi id: %s); falling back",
                city, bad or poi_ids,
            )
            return None
        lodging = lodging_by_id.get(lodging_id) if isinstance(lodging_id, str) else None
        if lodging is None:
            logger.error("DeepSeek fill FAILED for city=%s (lodging_id=%r not in catalog); falling back", city, lodging_id)
            return None
        seen_ids.extend(poi_ids)
        groups_by_local_day[day] = resolved
        lodging_by_local_day[day] = lodging

    if set(groups_by_local_day) != set(range(1, n_days + 1)):
        logger.error("DeepSeek fill FAILED for city=%s (days not 1..%s); falling back", city, n_days)
        return None
    repaired_sets = _repair_poi_set(groups_by_local_day, by_id, empty_ok)
    if repaired_sets is None:
        logger.error("DeepSeek fill FAILED for city=%s (poi set mismatch beyond repair); falling back", city)
        return None
    if repaired_sets:
        logger.info("DeepSeek poi-set repair city=%s %s", city, repaired_sets)
    if hotel_mode == "system_one" and len({l.id for l in lodging_by_local_day.values()}) != 1:
        logger.error("DeepSeek fill FAILED for city=%s (system_one used multiple hotels); falling back", city)
        return None
    if required_lodging_by_local_day is not None:
        for day, required in required_lodging_by_local_day.items():
            if lodging_by_local_day.get(day) is None or lodging_by_local_day[day].id != required.id:
                logger.error("DeepSeek fill FAILED for city=%s (custom lodging overridden); falling back", city)
                return None

    groups = [groups_by_local_day[d] for d in range(1, n_days + 1)]
    # A single misfit spot used to kill the whole model table (2026-08-28
    # prod: "district split" ×3 on tokyo). Repair obvious placements first;
    # only a still-broken table after repair falls back to rule-based.
    groups, district_moves = _repair_districts(groups)
    if district_moves:
        logger.info(
            "DeepSeek district repair city=%s moves=%s", city, district_moves,
        )
    if not _districts_ok(groups):
        logger.error("DeepSeek fill FAILED for city=%s (district split after repair); falling back", city)
        return None
    lodgings_by_local = {d: lodging_by_local_day[d] for d in range(1, n_days + 1)}
    return groups, lodgings_by_local


def _comfortable_spot_budget(days: int) -> int:
    """T-048: warning threshold = 3 x (days - 2) + 2.

    Derived from the capacity math (not a guess): a middle day's 540-minute
    budget minus 150 for lunch and dinner comfortably fits 3 spots; the
    arrival and departure days are travel days, ~2 spots between them. The
    old 5 x days threshold let users pick an itinerary guaranteed to overflow
    (17 spots / 6 days, hit in practice) and only warn after the fact.
    Still a soft warning — users may try, it never rejects."""
    return 3 * max(days - 2, 0) + 2


def plan_trip(
    *,
    cities: list[str],
    days: int,
    poi_ids: list[str],
    hotel_mode: str,
    custom_stays: list[CustomStayInput],
    poi_catalog: dict[str, list[Poi]],
    lodging_catalog: dict[str, list[Lodging]],
    deepseek_api_key: str = "",
    deepseek_base_url: str = "",
    first_day_density: str | None = None,
    last_day_density: str | None = None,
    edge_density: str | None = None,
    arrival_hub_id: str | None = None,
    departure_hub_id: str | None = None,
    hub_catalog: dict[str, list[TransportHub]] | None = None,
    # T-034: flight landing / takeoff times in minutes-from-midnight, local
    # to the arrival/departure hub's city. Only meaningful together with the
    # matching hub (main.py rejects orphan times).
    arrival_time_min: int | None = None,
    departure_time_min: int | None = None,
) -> TripPlan:
    """Raises :class:`PlanningError` for every validation failure in 2.6's
    400 table. Never raises for anything else — transit routing (T-012),
    DeepSeek filling (T-011), and the 10s cooldown (T-013) live elsewhere.
    """
    if len(cities) > MAX_CITIES:
        raise PlanningError("at most 4 cities")
    if not cities:
        raise PlanningError("at least one city is required")
    if days < len(cities):
        raise PlanningError("days must be >= number of cities")
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise PlanningError("days must be between 2 and 14")
    if hotel_mode not in ("system_multi", "system_one", "custom"):
        raise PlanningError("hotel_mode must be one of system_multi, system_one, custom")
    first_mode, last_mode = resolve_edge_modes(first_day_density, last_day_density, edge_density)

    hub_catalog = hub_catalog or {}

    def _resolve_hub(hub_id: str | None, city: str, role: str) -> TransportHub | None:
        if hub_id is None:
            return None
        match = next((h for h in hub_catalog.get(city, []) if h.id == hub_id), None)
        if match is None:
            raise PlanningError(f"unknown {role}_hub_id: {hub_id}")
        return match

    arrival_hub = _resolve_hub(arrival_hub_id, cities[0], "arrival")
    departure_hub = _resolve_hub(departure_hub_id, cities[-1], "departure")

    # T-034: turn the raw flight times into a day-1 clock start and a
    # last-day sightseeing cutoff. Applied only when the matching hub exists;
    # main.py already rejects orphan times, this is a second safety net.
    arrival_start_min = (
        arrival_time_min + LANDING_BUFFER_MIN
        if arrival_time_min is not None and arrival_hub is not None
        else None
    )
    departure_cutoff_min = (
        departure_time_min - TAKEOFF_BUFFER_MIN
        if departure_time_min is not None and departure_hub is not None
        else None
    )

    all_catalog_by_id: dict[str, Poi] = {
        poi.id: poi for city in cities for poi in poi_catalog.get(city, [])
    }
    unknown_poi_ids = [poi_id for poi_id in poi_ids if poi_id not in all_catalog_by_id]
    if unknown_poi_ids:
        raise PlanningError(f"unknown poi_ids: {unknown_poi_ids}")

    selected_by_city: dict[str, list[Poi]] = {city: [] for city in cities}
    for poi_id in poi_ids:
        poi = all_catalog_by_id[poi_id]
        selected_by_city[poi.city].append(poi)
    empty_cities = [city for city in cities if not selected_by_city[city]]
    if empty_cities:
        raise PlanningError("each city must have at least one selected poi")

    warnings: list[str] = []
    if len(poi_ids) > _comfortable_spot_budget(days):
        warnings.append(
            f"{len(poi_ids)} spots for {days} days is above the comfortable pace "
            f"(~{days - 2} transit-light days x 3 spots + 2 for arrival/departure) — "
            "expect some spots to be squeezed out; add a day or drop a spot"
        )

    poi_counts = {city: len(selected_by_city[city]) for city in cities}
    city_days = _split_city_days(cities, days, poi_counts)
    day_city: dict[int, str] = {d: city for city, day_list in city_days.items() for d in day_list}

    # T-016: trip's global first/last day are always city_days[cities[0]][0]
    # (=day 1) and city_days[cities[-1]][-1] (=days) — _split_city_days keeps
    # day ranges in the same order as ``cities``.
    first_day_global = city_days[cities[0]][0]
    last_day_global = city_days[cities[-1]][-1]

    # T-045: every day's clock start (arrival day post-landing, intercity
    # arrival days 16:00, plain days 09:00) — the closing-time final check
    # replays each day against these so "on the route but closed" never
    # survives to the response.
    day_clock_starts: dict[int, int] = {}
    for city, day_list in city_days.items():
        block_start = day_list[0]
        for i, d in enumerate(day_list):
            if i == 0 and first_day_global in day_list and arrival_start_min is not None:
                day_clock_starts[d] = arrival_start_min
            elif i == 0 and block_start > 1 and day_city.get(block_start - 1) != city:
                # T-049b: 16:00 on an intercity day is only the floor for
                # "intercity transit done" — after arrival there is still
                # baggage storage / transfers / the first leg into town
                # (~60 min) before sightseeing really starts. That is how
                # kinkaku-ji (closes 17:00) got its phantom "reachable at
                # 16:50" slot on an intercity day (real arrival 18:15+).
                day_clock_starts[d] = INTERCITY_MIN + _INTERCITY_ARRIVAL_BUFFER_MIN
            else:
                day_clock_starts[d] = DAY_START_MIN

    if hotel_mode == "custom":
        flat_days = [d for stay in custom_stays for d in stay.days]
        if sorted(flat_days) != list(range(1, days + 1)):
            raise PlanningError("custom_stays must cover every day")
        lodging_by_day: dict[int, Lodging] = {}
        for stay in custom_stays:
            city_for_stay_days = {day_city[d] for d in stay.days}
            resolved: Lodging | None = None
            for city in city_for_stay_days:
                match = next((l for l in lodging_catalog.get(city, []) if l.id == stay.lodging_id), None)
                if match is not None:
                    resolved = match
                    break
            if resolved is None:
                raise PlanningError(f"unknown lodging_id: {stay.lodging_id}")
            for d in stay.days:
                lodging_by_day[d] = resolved
    else:
        lodging_by_day = {}

    # B + D combined, per city (T-011): DeepSeek gets first shot at filling
    # this city's days (grouping *and* lodging together, since custom mode
    # needs the lodging as a grouping anchor). Any validation miss against
    # 6.1b F falls back to the rule-based v1 algorithm for *that city only*
    # — one city's bad reply never sinks the whole trip.
    day_groups: dict[int, list[Poi]] = {}
    use_deepseek = bool(deepseek_api_key)  # no key -> never even attempt HTTP
    city_groupers: dict[str, str] = {}

    # T-037: per-city context pass so every DeepSeek fill can run in
    # parallel — each call is a pure HTTP round-trip with no shared state.
    city_ctx: dict[str, dict] = {}
    for city, day_list in city_days.items():
        n_days = len(day_list)
        pois = selected_by_city[city]
        candidates = lodging_catalog.get(city, [])
        if hotel_mode in ("system_one", "system_multi") and not candidates:
            raise PlanningError(f"no lodgings available for city '{city}'")
        required_local: dict[int, Lodging] | None = None
        if hotel_mode == "custom":
            required_local = {i + 1: lodging_by_day[d] for i, d in enumerate(day_list)}
        # T-016: first/last density apply only when this city owns that
        # global day. Computed before DeepSeek so the prompt can use them.
        first_mode_here = first_mode if first_day_global in day_list else None
        last_mode_here = last_mode if last_day_global in day_list else None
        # T-034: flight-aware per-day load budgets + open-hours feasibility
        # windows (same ownership logic; intercity arrival days get one too).
        budgets = _flight_day_budgets(
            n_days,
            owns_global_first_day=first_day_global in day_list,
            owns_global_last_day=last_day_global in day_list,
            arrival_start_min=arrival_start_min,
            departure_cutoff_min=departure_cutoff_min,
        )
        windows: dict[int, tuple[int, int]] = {}
        if first_day_global in day_list and arrival_start_min is not None:
            windows[0] = (arrival_start_min, DAY_END_MIN)
        if last_day_global in day_list and departure_cutoff_min is not None:
            windows[n_days - 1] = (DAY_START_MIN, departure_cutoff_min)
        block_start = day_list[0]
        if block_start > 1 and day_city.get(block_start - 1) != city:
            windows[0] = (INTERCITY_MIN, DAY_END_MIN)
        city_ctx[city] = {
            "n_days": n_days,
            "pois": pois,
            "candidates": candidates,
            "required_local": required_local,
            "first_mode_here": first_mode_here,
            "last_mode_here": last_mode_here,
            "budgets": budgets,
            "windows": windows,
        }

    deepseek_results: dict[str, tuple[list[list[Poi]], dict[int, Lodging]] | None] = {}
    if use_deepseek:
        jobs: dict[str, dict] = {}
        for city, ctx in city_ctx.items():
            jobs[city] = dict(
                city=city,
                pois=ctx["pois"],
                n_days=ctx["n_days"],
                hotel_mode=hotel_mode,
                candidates=ctx["candidates"],
                required_lodging_by_local_day=ctx["required_local"],
                api_key=deepseek_api_key,
                base_url=deepseek_base_url,
                first_mode=ctx["first_mode_here"],
                last_mode=ctx["last_mode_here"],
                arrival_start_min=arrival_start_min if first_day_global in city_days[city] else None,
                departure_cutoff_min=departure_cutoff_min if last_day_global in city_days[city] else None,
                arrival_hub=arrival_hub,
                departure_hub=departure_hub,
            )

        def _fill_with_retry(kwargs: dict) -> tuple[list[list[Poi]], dict[int, Lodging]] | None:
            # T-046b: even at temperature 0.2 the model still occasionally
            # emits bad JSON / near-miss validation (~1/3 of calls). Each
            # sample differs, so one automatic retry roughly halves the
            # fallback rate — the cost is a single extra model call.
            result = _deepseek_fill_city(**kwargs)
            if result is None:
                logger.warning("DeepSeek fill failed once for city=%s; retrying", kwargs["city"])
                result = _deepseek_fill_city(**kwargs)
            return result

        with ThreadPoolExecutor(max_workers=min(len(city_ctx), 4)) as pool:
            futures = {city: pool.submit(_fill_with_retry, kw) for city, kw in jobs.items()}
            deepseek_results = {city: fut.result() for city, fut in futures.items()}

    # T-049b: spots stuck at the windows stage are held back, not warned yet —
    # the final check may still rescue them; rescued ones retract the warning,
    # the rest are reported with one consistent message.
    window_stuck_ids: set[str] = set()

    for city, day_list in city_days.items():
        ctx = city_ctx[city]
        n_days = ctx["n_days"]
        pois = ctx["pois"]
        budgets = ctx["budgets"]
        windows = ctx["windows"]
        first_mode_here = ctx["first_mode_here"]
        last_mode_here = ctx["last_mode_here"]
        result = deepseek_results.get(city) if use_deepseek else None
        empty_idx = _empty_day_indices(n_days, first_mode_here, last_mode_here)
        if result is not None:
            groups, lodging_by_local = result
            city_groupers[city] = "deepseek"
            # Density/geography were in the prompt. Still enforce capacity and
            # empty none-days in Python — the model is allowed to miss.
            groups = _isolate_full_day_pois(groups)
            # Evacuate none-days FIRST, then check open-hours windows — a spot
            # feasibility saw on a legal day could get dumped onto a window day
            # by the evacuation and never re-checked (kinkaku-ji 2026-08-28).
            groups = _pack_day_capacity(groups, empty_idx=empty_idx)
            groups, _moved, stuck = _move_infeasible_for_windows(groups, windows, empty_idx)
            window_stuck_ids.update(stuck)  # hold back; no warning if the final check rescues it
            groups = _pack_day_capacity(groups, empty_idx=empty_idx, budget_by_idx=budgets)
        else:
            city_groupers[city] = "rule-based"
            groups = _group_city_pois(pois, n_days)
            groups = _pack_day_capacity(groups, empty_idx=empty_idx, budget_by_idx=budgets)
            if first_mode_here is not None or last_mode_here is not None:
                groups = _rebalance_edge_days(
                    groups, first_mode=first_mode_here, last_mode=last_mode_here,
                )
            groups = _isolate_full_day_pois(groups)
            groups = _pack_day_capacity(groups, empty_idx=empty_idx)
            groups, _moved, stuck = _move_infeasible_for_windows(groups, windows, empty_idx)
            window_stuck_ids.update(stuck)  # hold back; no warning if the final check rescues it
            groups = _pack_day_capacity(groups, empty_idx=empty_idx, budget_by_idx=budgets)
            if hotel_mode == "system_one":
                chosen = _select_hotels_system_one(candidates, groups)
                lodging_by_local = {i + 1: chosen for i in range(n_days)}
            elif hotel_mode == "system_multi":
                lodging_by_local = _select_hotels_system_multi(candidates, list(range(1, n_days + 1)), groups)
            else:  # custom — lodging is already fixed by the user
                lodging_by_local = required_local

        # T-045: closing-time final check (runs after the two branches converge
        # — the day's hotel is settled by then). The arrival day's first leg is
        # folded into the clock start as "20 min check-in + 1.2 min/km rail",
        # so the flat 25 min/hop approximation no longer blesses phantom
        # "16:20 at Ueno" style plans.
        if day_list and arrival_hub is not None and first_day_global in day_list:
            hotel = lodging_by_local[1]
            if arrival_hub.kind == "airport":
                commute = _AIRPORT_COMMUTE_MIN
            else:
                commute = _COMMUTE_BASE_MIN + _COMMUTE_PER_KM_MIN * haversine_km(
                    arrival_hub.lat, arrival_hub.lng, hotel.lat, hotel.lng,
                )
            day_clock_starts[day_list[0]] += int(_CHECKIN_MIN + commute)
        starts_local = {i: day_clock_starts[d] for i, d in enumerate(day_list)}
        ends_local: dict[int, int] = {}
        for i, d in enumerate(day_list):
            cap = DAY_END_MIN
            if d == last_day_global and departure_cutoff_min is not None:
                cap = min(cap, departure_cutoff_min)
            ends_local[i] = cap
        # Each day's first hop origin = that morning's hotel (6.1b E: on day 1
        # the morning uses that night's lodging)
        first_from_local: dict[int, tuple[float, float] | None] = {}
        for i in range(len(day_list)):
            morning = lodging_by_local[i] if i > 0 else lodging_by_local[1]
            first_from_local[i] = (morning.lat, morning.lng)
        groups, close_moves, close_stuck = _enforce_closing(
            groups, starts_local, empty_idx, budgets, first_from_local, ends_local,
        )
        if close_moves:  # T-054: these used to be silent — undiagnosable in prod
            logger.info("Closing-check moves city=%s %s", city, close_moves)
        # T-054 final-state police: packing/closing rescues can scatter a spot
        # across town AFTER the model table passed the district check (the
        # destination days they pick may be the only budget-feasible ones).
        # Repair the final grouping, then re-run the closing check so the
        # repaired state is verified against the same bar — its stuck list
        # replaces the first pass's.
        groups, post_moves = _repair_districts(groups)
        if post_moves:
            logger.info("Post-closing district repair city=%s %s", city, post_moves)
            groups, _m2, close_stuck = _enforce_closing(
                groups, starts_local, empty_idx, budgets, first_from_local, ends_local,
            )
        # Warnings held back at the windows stage: retract the ones the final
        # check rescued (anything outside close_stuck), report the rest with
        # one consistent message
        stale = {
            f"{uid} cannot fit within opening hours on its assigned day"
            for uid in window_stuck_ids
            if uid not in close_stuck
        }
        warnings = [w for w in warnings if w not in stale]
        for poi_id in close_stuck:
            warnings.append(
                f"{poi_id} doesn't fit its day (would arrive after closing or past the day's "
                "cutoff) — the trip is packed; move it to another day on the map or drop a spot"
            )
        # T-051: arrival-day anchoring - landing evening stays within 5km of
        # the hotel; far spots return to their geographic day; if the arrival
        # day empties, pull in the nearest spot.
        if first_day_global in day_list and arrival_hub is not None:
            groups, prox_moved = _enforce_arrival_proximity(
                groups, starts_local, ends_local, first_from_local,
                (lodging_by_local[1].lat, lodging_by_local[1].lng),
            )
            if prox_moved:
                logger.info("Arrival-day proximity moves: %s", prox_moved)

        for i, day in enumerate(day_list):
            day_groups[day] = groups[i]
            if hotel_mode != "custom":
                lodging_by_day[day] = lodging_by_local[i + 1]

    groupers_used = set(city_groupers.values())
    if groupers_used == {"deepseek"}:
        grouper_name = "deepseek"
    elif groupers_used == {"rule-based"}:
        grouper_name = "rule-based"
    else:
        grouper_name = "mixed"

    planned_days: list[PlannedDay] = []
    for day in range(1, days + 1):
        night = lodging_by_day[day]
        morning = lodging_by_day[day - 1] if day > 1 else night  # 6.1b E: day 1 simplification
        end = DAY_END_MIN
        if day == last_day_global and departure_cutoff_min is not None:
            end = min(end, departure_cutoff_min)
        planned_days.append(PlannedDay(
            day=day,
            city=day_city[day],
            # T-039 2-opt + T-036 early-closing ordering, then T-045: replay
            # the day clock and pull spots that would arrive after closing
            # toward the front — "on the route but closed" stays a warning
            # of last resort, not a design outcome.
            pois=_reorder_for_closing(
                _order_day(day_groups[day]),
                day_clock_starts.get(day, DAY_START_MIN),
                (morning.lat, morning.lng),
                end,
            ),
            lodging=night,
            morning_lodging=morning,
        ))

    selected_ids = set(poi_ids)
    unselected = [
        poi.id
        for city in cities
        for poi in poi_catalog.get(city, [])
        if poi.id not in selected_ids
    ]

    return TripPlan(
        days=planned_days,
        unselected_poi_ids=unselected,
        warnings=warnings,
        grouper=grouper_name,
        arrival_hub=arrival_hub,
        departure_hub=departure_hub,
    )
