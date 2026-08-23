"""Rule-based itinerary planner v1 (T-010, 方案计划.md 6.1b).

Turns a multi-city trip request into a day-by-day plan: which city each day
belongs to, which POIs happen that day (ordered), and which lodging that
night — everything `POST /optimize-route` needs before transit legs get
queried. DeepSeek per-city filling (2.8) is T-011: this module both is the
fallback when the model is unavailable and defines the shape/validation the
model's JSON must satisfy.

All 400 `detail` strings here are frozen by 实现批次.md 2.6 — the frontend
matches on them, so don't reword.
"""

from __future__ import annotations

import logging
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
from .lodging import Lodging
from .poi import Poi
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
REASONABLE_DAY_POIS = 5
IDEAL_FEW_POIS = 1
# Parks / mountains that fill a calendar day. Do not mix with other POIs.
FULL_DAY_DURATION_MIN = 360


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
    """Wraps :class:`RuleBasedGrouper`, which caps its group count at
    ``len(pois)``. When this city has fewer selected spots than days
    assigned to it (a demo edge case, not the common path), each spot gets
    its own day, spread evenly across the block, and the remaining days are
    left with no poi (pure hotel/transit) — an earlier version cycled back
    through the same spots to avoid empty days, but that meant re-visiting
    a spot on a later day, which is worse than a quiet transit day (bug
    report 2026-08-21: "Senso-ji Temple" showed up on both day 1 and day 4).
    """
    if n_days <= len(pois):
        return RuleBasedGrouper().group(pois, n_days)
    ordered = [group[0] for group in RuleBasedGrouper().group(pois, len(pois))]
    groups: list[list[Poi]] = [[] for _ in range(n_days)]
    for i, poi in enumerate(ordered):
        slot = min(round((i + 0.5) * n_days / len(ordered)), n_days - 1)
        groups[slot] = [poi]
    return groups


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
        if overflow and few_idx:
            extra = _even_split(len(few_idx), overflow)
            for i, add in zip(few_idx, extra):
                counts[i] += add
            overflow = 0
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


def _deepseek_city_prompt(
    city: str,
    pois: list[Poi],
    n_days: int,
    hotel_mode: str,
    candidates: list[Lodging],
    required_lodging_by_local_day: dict[int, Lodging] | None,
    first_mode: str | None = None,
    last_mode: str | None = None,
) -> str:
    poi_lines = "\n".join(
        f"- {p.id}: {p.name_en} ({p.lat:.4f},{p.lng:.4f}, {p.suggested_duration_min} min)"
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
    ]
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
        parsed = _extract_json(text)
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
            logger.error("DeepSeek fill FAILED for city=%s (unknown or duplicate poi id); falling back", city)
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
    if set(seen_ids) != set(by_id) or len(seen_ids) != len(by_id):
        logger.error("DeepSeek fill FAILED for city=%s (poi set mismatch); falling back", city)
        return None
    if hotel_mode == "system_one" and len({l.id for l in lodging_by_local_day.values()}) != 1:
        logger.error("DeepSeek fill FAILED for city=%s (system_one used multiple hotels); falling back", city)
        return None
    if required_lodging_by_local_day is not None:
        for day, required in required_lodging_by_local_day.items():
            if lodging_by_local_day.get(day) is None or lodging_by_local_day[day].id != required.id:
                logger.error("DeepSeek fill FAILED for city=%s (custom lodging overridden); falling back", city)
                return None

    groups = [groups_by_local_day[d] for d in range(1, n_days + 1)]
    lodgings_by_local = {d: lodging_by_local_day[d] for d in range(1, n_days + 1)}
    return groups, lodgings_by_local


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
    if len(poi_ids) > days * 5:
        warnings.append("selected a lot of POIs for the number of days — days may feel crowded")

    poi_counts = {city: len(selected_by_city[city]) for city in cities}
    city_days = _split_city_days(cities, days, poi_counts)
    day_city: dict[int, str] = {d: city for city, day_list in city_days.items() for d in day_list}

    # T-016: trip's global first/last day are always city_days[cities[0]][0]
    # (=day 1) and city_days[cities[-1]][-1] (=days) — _split_city_days keeps
    # day ranges in the same order as ``cities``.
    first_day_global = city_days[cities[0]][0]
    last_day_global = city_days[cities[-1]][-1]

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

        result = None
        if use_deepseek:
            result = _deepseek_fill_city(
                city=city,
                pois=pois,
                n_days=n_days,
                hotel_mode=hotel_mode,
                candidates=candidates,
                required_lodging_by_local_day=required_local,
                api_key=deepseek_api_key,
                base_url=deepseek_base_url,
                first_mode=first_mode_here,
                last_mode=last_mode_here,
            )

        if result is not None:
            groups, lodging_by_local = result
            city_groupers[city] = "deepseek"
            # Density and geography were in the prompt. Do not count-rebalance
            # an accepted AI table — that was why DeepSeek "had no effect".
            groups = _isolate_full_day_pois(groups)
        else:
            city_groupers[city] = "rule-based"
            groups = _group_city_pois(pois, n_days)
            if first_mode_here is not None or last_mode_here is not None:
                groups = _rebalance_edge_days(
                    groups, first_mode=first_mode_here, last_mode=last_mode_here,
                )
            groups = _isolate_full_day_pois(groups)
            if hotel_mode == "system_one":
                chosen = _select_hotels_system_one(candidates, groups)
                lodging_by_local = {i + 1: chosen for i in range(n_days)}
            elif hotel_mode == "system_multi":
                lodging_by_local = _select_hotels_system_multi(candidates, list(range(1, n_days + 1)), groups)
            else:  # custom — lodging is already fixed by the user
                lodging_by_local = required_local

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
        planned_days.append(PlannedDay(
            day=day,
            city=day_city[day],
            pois=day_groups[day],
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
