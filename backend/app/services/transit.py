"""Transit routing data layer.

Two implementations behind one interface (D-010):

- :class:`SerpApiTransitProvider` — live ``google_maps_directions`` calls
  (``travel_mode=3``, public transit) with a file cache so identical
  requests never hit the API twice.
- :class:`LocalTransitProvider` — reads the same cache directory; on a miss
  returns a straight-line-distance estimate flagged ``estimated=True`` so the
  frontend can render estimates differently from real transit data.

Both read and write ``data/transit_cache/``, which is committed to git: one
paid online run becomes the offline dataset, and judges can clone and run the
project without an API key of their own.

Response shape verified against live Tokyo data on 2026-08-13. The payload has
no ``routes`` key — it is ``directions``, a list of alternative options, each
with ``cost``/``currency`` and a ``trips`` list of walking and transit legs.
Transit legs carry the line in ``title`` plus ``start_stop``/``end_stop``/``stops``.
"""

from __future__ import annotations

import logging
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import BaseModel

from .currency import DISPLAY_CURRENCY, to_usd

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0

# Taxi fallback (T-020): road-ish distance and a labelled fare in USD.
# Not a metro ticket. Flag + per-km are backpacker round numbers.
TAXI_SPEED_KMH = 22.0
TAXI_WAIT_MIN = 3
TAXI_ROAD_FACTOR = 1.3
TAXI_FARE_USD = {
    "JP": (3.5, 2.2),
    "CN": (1.8, 0.40),
    "KR": (2.4, 0.85),
    "HK": (3.0, 1.1),
}

# T-012 / plan 7.3: straight-line hops shorter than this never call SerpApi —
# a paid transit query for two spots on the same block wastes quota and
# usually returns a worse answer than "just walk there" anyway.
WALKING_SHORT_CIRCUIT_KM = 0.8
WALKING_SPEED_KMH = 4.5

# How much slower than the quickest option a route may be before it stops
# counting as a sensible way to save money. See :func:`_pick_best`.
MAX_DETOUR_RATIO = 1.6


class Coord(BaseModel):
    lat: float
    lng: float


class TransitLeg(BaseModel):
    travel_mode: str = "transit"  # "transit" | "walking" | "taxi"
    line_name: str = ""           # e.g. "Ginza Line Local Shibuya"
    from_stop: str = ""           # e.g. "Asakusa Sta."
    to_stop: str = ""             # e.g. "Shibuya Station"
    stop_count: int = 0           # intermediate stops, for "12 stops" in the UI
    start_time: str = ""
    end_time: str = ""
    duration_min: int = 0
    cost: float = 0.0
    estimated: bool = False


class RouteOption(BaseModel):
    """A compact alternative route, for the cheapest-vs-fastest comparison.

    SerpApi returns several real options per pair with genuine trade-offs
    (Tokyo Station to Ginza: 9 min for JPY 180, or 11 min for JPY 160), which
    is the whole point for a budget traveller.
    """

    duration_min: int
    cost: float
    transfer_count: int
    line_summary: str = ""  # e.g. "Ginza Line + Hibiya Line"


class TransitRoute(BaseModel):
    total_duration_min: int
    total_cost: float
    currency: str = "JPY"
    transfer_count: int
    legs: list[TransitLeg]
    estimated: bool = False
    frequency: str = ""                        # e.g. "every 5 min"
    alternatives: list[RouteOption] = []       # excludes the chosen route
    # How this route was produced. Old cache files omit the field — default
    # is SerpApi's on-disk response, not a live HTTP call.
    data_source: str = "serpapi_cache"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two coordinates, in kilometers."""
    rlat1, rlng1 = math.radians(lat1), math.radians(lng1)
    rlat2, rlng2 = math.radians(lat2), math.radians(lng2)
    dlat, dlng = rlat2 - rlat1, rlng2 - rlng1
    h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def country_for(lat: float, lng: float, city: str = "") -> str:
    if city in {"shanghai", "beijing"}:
        return "CN"
    if city == "hongkong":
        return "HK"
    if city in {"seoul", "busan", "jeju"}:
        return "KR"
    if city in {"tokyo", "kyoto", "osaka"}:
        return "JP"
    if lng >= 124:
        return "KR" if lng < 132 else "JP"
    if lat < 23.5 and lng < 115.5:
        return "HK"
    if 108 <= lng < 124:
        return "CN"
    return "JP"


def is_mainland_china(lat: float, lng: float, city: str = "") -> bool:
    return country_for(lat, lng, city) == "CN"


def taxi_estimate(
    origin: Coord,
    dest: Coord,
    *,
    country: str = "JP",
    duration_min: int | None = None,
    road_km: float | None = None,
) -> TransitRoute:
    """Honest taxi fallback (T-020): duration + USD fare, labelled estimated."""
    straight = haversine_km(origin.lat, origin.lng, dest.lat, dest.lng)
    km = road_km if road_km is not None else straight * TAXI_ROAD_FACTOR
    if duration_min is None:
        duration_min = max(5, round(km / TAXI_SPEED_KMH * 60) + TAXI_WAIT_MIN)
    flag, per_km = TAXI_FARE_USD.get(country, TAXI_FARE_USD["JP"])
    cost = round(flag + per_km * km, 2)
    leg = TransitLeg(
        travel_mode="taxi",
        duration_min=duration_min,
        cost=cost,
        estimated=True,
    )
    return TransitRoute(
        total_duration_min=duration_min,
        total_cost=cost,
        currency=DISPLAY_CURRENCY,
        transfer_count=0,
        legs=[leg],
        estimated=True,
        data_source="taxi",
    )


def estimate_route(origin: Coord, dest: Coord, *, city: str = "") -> TransitRoute:
    """Exception-path fallback: same numbers as taxi, tagged ``estimate``."""
    route = taxi_estimate(origin, dest, country=country_for(origin.lat, origin.lng, city))
    route.data_source = "estimate"
    return route


def route_in_usd(route: TransitRoute) -> TransitRoute:
    if (route.currency or DISPLAY_CURRENCY).upper() == DISPLAY_CURRENCY:
        route.currency = DISPLAY_CURRENCY
        return route
    src = route.currency
    route.total_cost = to_usd(route.total_cost, src) or 0.0
    for alt in route.alternatives:
        alt.cost = to_usd(alt.cost, src) or 0.0
    for leg in route.legs:
        if leg.cost:
            leg.cost = to_usd(leg.cost, src) or 0.0
    route.currency = DISPLAY_CURRENCY
    return route


def _is_stale_no_timetable(route: TransitRoute) -> bool:
    """Old cache: estimated metro with cost 0. Retry live, then rewrite as taxi."""
    if not route.estimated:
        return False
    modes = {leg.travel_mode for leg in route.legs}
    return modes <= {"transit"} and (route.total_cost or 0) == 0


def _walking_route(distance_km: float) -> TransitRoute:
    """Straight-line walking estimate — never calls SerpApi (T-012 6.1.7.3)."""
    duration_min = max(1, round(distance_km / WALKING_SPEED_KMH * 60))
    leg = TransitLeg(travel_mode="walking", duration_min=duration_min, cost=0.0, estimated=True)
    return TransitRoute(
        total_duration_min=duration_min,
        total_cost=0.0,
        currency=DISPLAY_CURRENCY,
        transfer_count=0,
        legs=[leg],
        estimated=True,
        data_source="walk",
    )


def _maybe_walking(origin: Coord, dest: Coord) -> TransitRoute | None:
    distance_km = haversine_km(origin.lat, origin.lng, dest.lat, dest.lng)
    if distance_km < WALKING_SHORT_CIRCUIT_KM:
        return _walking_route(distance_km)
    return None


def _route_cache_path(cache_dir: Path, origin: Coord, dest: Coord, hour_bucket: str = "") -> Path:
    """Deterministic cache key: origin/destination rounded to ~11 m, plus an
    optional departure-hour bucket (T-012: "09" intra-city / "16" intercity).

    ``hour_bucket=""`` reproduces the pre-T-012 key exactly, so callers that
    don't care about time-of-day (``scripts/fetch_transit.py``, and the
    fallback lookup below) keep reading the committed demo cache untouched.
    """
    key = f"route_{origin.lat:.4f}_{origin.lng:.4f}_{dest.lat:.4f}_{dest.lng:.4f}"
    if hour_bucket:
        key += f"_{hour_bucket}"
    return cache_dir / f"{key}.json"


def _duration_min(option: dict) -> int:
    return max(1, round((option.get("duration") or 0) / 60))


def _line_summary(option: dict) -> str:
    """Human-readable chain of transit lines, e.g. "Ginza Line + Hibiya Line"."""
    titles = [
        trip.get("title", "")
        for trip in option.get("trips") or []
        if str(trip.get("travel_mode", "")).lower() != "walking"
    ]
    return " + ".join(t for t in titles if t)


def _transfer_count(option: dict) -> int:
    transit_legs = [
        trip
        for trip in option.get("trips") or []
        if str(trip.get("travel_mode", "")).lower() != "walking"
    ]
    return max(0, len(transit_legs) - 1)


def _summarize(option: dict) -> RouteOption:
    return RouteOption(
        duration_min=_duration_min(option),
        cost=float(option.get("cost") or 0),
        transfer_count=_transfer_count(option),
        line_summary=_line_summary(option),
    )


def _pick_best(options: list[dict]) -> dict:
    """Cheapest option that isn't absurdly slow — the budget traveller's answer.

    Google mixes in genuine outliers: an Ueno→Roppongi query returns a 39 min
    route alongside a 2 h 14 min one. Picking on price alone would strand the
    user on a train for two hours to save a few hundred yen, so anything much
    slower than the quickest option is discarded before comparing fares.
    """
    fastest = min(_duration_min(opt) for opt in options)
    sensible = [opt for opt in options if _duration_min(opt) <= fastest * MAX_DETOUR_RATIO]
    return min(sensible, key=lambda opt: (float(opt.get("cost") or 0), _duration_min(opt)))


class TransitProvider(ABC):
    """Interface: resolve a public-transit route between two coordinates.

    ``depart_at``/``hour_bucket`` are optional (T-012): omitting both keeps
    the pre-T-012 behaviour (no time-of-day query, legacy cache key) for
    callers like ``scripts/fetch_transit.py`` that pre-seed the cache without
    caring about departure time.
    """

    name: str = "unknown"

    @abstractmethod
    def get_route(
        self,
        origin: Coord,
        dest: Coord,
        *,
        depart_at: int | None = None,
        hour_bucket: str = "",
        city: str = "",
    ) -> TransitRoute:
        ...


class SerpApiTransitProvider(TransitProvider):
    """Live routes via SerpApi ``google_maps_directions`` with a file cache.

    Cache key is the (rounded) origin/destination pair, so the same request
    never hits the API twice. Writes to ``data/transit_cache/`` — the same
    directory :class:`LocalTransitProvider` reads, so a paid online run
    doubles as the committed offline dataset. Delete a file to force refresh.
    """

    name = "serpapi"

    def __init__(self, api_key: str, cache_dir: Path):
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_route(
        self,
        origin: Coord,
        dest: Coord,
        *,
        depart_at: int | None = None,
        hour_bucket: str = "",
        city: str = "",
    ) -> TransitRoute:
        walking = _maybe_walking(origin, dest)
        if walking is not None:
            return walking

        cache_file = _route_cache_path(self._cache_dir, origin, dest, hour_bucket)
        cached: TransitRoute | None = None
        if cache_file.exists():
            try:
                cached = TransitRoute.model_validate_json(cache_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("ignoring corrupt route cache %s: %s", cache_file, exc)

        if cached is None and hour_bucket:
            legacy_file = _route_cache_path(self._cache_dir, origin, dest)
            if legacy_file.exists():
                try:
                    cached = TransitRoute.model_validate_json(legacy_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("ignoring corrupt legacy route cache %s: %s", legacy_file, exc)

        if cached is not None and not _is_stale_no_timetable(cached):
            cached.data_source = "serpapi_cache"
            return route_in_usd(cached)

        try:
            route = self._fetch(origin, dest, depart_at=depart_at)
            route.data_source = "serpapi_live"
        except Exception as exc:
            logger.warning("SerpApi transit miss (%s); taxi estimate", exc)
            route = taxi_estimate(origin, dest, country=country_for(origin.lat, origin.lng, city))
        # T-046: once transit queries run in parallel, threads can write the
        # same cache file at once — write to a temp file + atomic rename so
        # interleaved writes can never corrupt the JSON (a corrupt file only
        # costs one extra query, but there is no reason to allow it).
        tmp = cache_file.with_name(cache_file.name + ".tmp")
        tmp.write_text(route.model_dump_json(), encoding="utf-8")
        os.replace(tmp, cache_file)
        return route_in_usd(route)

    def _fetch(self, origin: Coord, dest: Coord, *, depart_at: int | None = None) -> TransitRoute:
        params = {
            "engine": "google_maps_directions",
            "travel_mode": 3,  # public transit
            "start_addr": f"{origin.lat},{origin.lng}",
            "end_addr": f"{dest.lat},{dest.lng}",
            "hl": "en",
            "api_key": self._api_key,
        }
        if depart_at is not None:
            params["time"] = f"depart_at:{depart_at}"
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        options = [
            opt
            for opt in (data.get("directions") or [])
            if str(opt.get("travel_mode", "")).lower() == "transit"
        ]
        if not options:
            modes = [opt.get("travel_mode") for opt in (data.get("directions") or [])][:8]
            raise RuntimeError(
                f"SerpApi returned no transit options (error={data.get('error')!r}, travel_modes={modes})"
            )

        chosen = _pick_best(options)
        others = [_summarize(opt) for opt in options if opt is not chosen]
        legs = [self._parse_leg(trip) for trip in chosen.get("trips") or []]
        transit_legs = [leg for leg in legs if leg.travel_mode == "transit"]

        return TransitRoute(
            total_duration_min=_duration_min(chosen),
            total_cost=float(chosen.get("cost") or 0),
            currency=chosen.get("currency") or "JPY",  # converted to USD on the way out
            transfer_count=max(0, len(transit_legs) - 1),
            legs=legs,
            frequency=chosen.get("via") or "",
            alternatives=others,
        )

    def _parse_leg(self, trip: dict) -> TransitLeg:
        mode = "walking" if str(trip.get("travel_mode", "")).lower() == "walking" else "transit"
        start_stop = trip.get("start_stop") or {}
        end_stop = trip.get("end_stop") or {}
        return TransitLeg(
            travel_mode=mode,
            # Walking legs are titled just "Walk"; only transit legs carry a line.
            line_name="" if mode == "walking" else (trip.get("title") or ""),
            from_stop=start_stop.get("name", ""),
            to_stop=end_stop.get("name", ""),
            stop_count=len(trip.get("stops") or []),
            start_time=start_stop.get("time", ""),
            end_time=end_stop.get("time", ""),
            duration_min=max(1, round((trip.get("duration") or 0) / 60)),
        )


class LocalTransitProvider(TransitProvider):
    """Offline provider: precomputed JSON in ``data/transit_cache/``.

    A cache miss falls back to a straight-line-distance estimate marked
    ``estimated=True`` — the frontend must render estimates differently from
    real transit data.
    """

    name = "local-json"

    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir

    def get_route(
        self,
        origin: Coord,
        dest: Coord,
        *,
        depart_at: int | None = None,
        hour_bucket: str = "",
        city: str = "",
    ) -> TransitRoute:
        walking = _maybe_walking(origin, dest)
        if walking is not None:
            return walking
        precomputed = self._load_precomputed(origin, dest, hour_bucket)
        if precomputed is not None and not _is_stale_no_timetable(precomputed):
            precomputed.data_source = "serpapi_cache"
            return route_in_usd(precomputed)
        return taxi_estimate(origin, dest, country=country_for(origin.lat, origin.lng, city))

    def _load_precomputed(self, origin: Coord, dest: Coord, hour_bucket: str = "") -> TransitRoute | None:
        cache_file = _route_cache_path(self._cache_dir, origin, dest, hour_bucket)
        if not cache_file.exists() and hour_bucket:
            # Same legacy fallback as SerpApiTransitProvider — an hour-less
            # precomputed entry is still valid data, just from before T-012.
            cache_file = _route_cache_path(self._cache_dir, origin, dest)
        if not cache_file.exists():
            return None
        try:
            return TransitRoute.model_validate_json(cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("ignoring unreadable precomputed route %s: %s", cache_file, exc)
            return None

    def _estimate(self, origin: Coord, dest: Coord) -> TransitRoute:
        return estimate_route(origin, dest)
