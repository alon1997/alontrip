"""Mainland China transit via the Amap (Gaode) web service (D-020).

SerpApi/Google transit is empty in Shanghai/Beijing. This talks to
``direction/transit/integrated`` (bus/metro) and, if that is empty, uses
the taxi_cost / driving duration Amap already returns.

Coordinates are **lng,lat** — opposite of SerpApi.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .currency import to_usd
from .transit import (
    Coord,
    TransitLeg,
    TransitRoute,
    _maybe_walking,
    _route_cache_path,
    taxi_estimate,
)

logger = logging.getLogger(__name__)

AMAP_TRANSIT = "https://restapi.amap.com/v3/direction/transit/integrated"
AMAP_DRIVING = "https://restapi.amap.com/v3/direction/driving"

# citycode: 010 Beijing / 021 Shanghai. Names also work; codes are stabler.
AMAP_CITY = {
    "beijing": "010",
    "shanghai": "021",
}


def amap_city_code(city: str, lat: float, lng: float) -> str:
    if city in AMAP_CITY:
        return AMAP_CITY[city]
    return "010" if lat >= 36 else "021"


class AmapTransitProvider:
    name = "amap"

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
        if cache_file.exists():
            try:
                cached = TransitRoute.model_validate_json(cache_file.read_text(encoding="utf-8"))
                if not cached.estimated or any(leg.travel_mode == "taxi" for leg in cached.legs):
                    if any(leg.travel_mode == "taxi" for leg in cached.legs):
                        cached.data_source = "taxi"
                    else:
                        cached.data_source = "amap"
                    return cached
            except Exception as exc:
                logger.warning("ignoring corrupt Amap cache %s: %s", cache_file, exc)

        try:
            route = self._fetch_transit(origin, dest, city=city)
        except Exception as exc:
            logger.warning("Amap transit miss (%s); trying driving/taxi", exc)
            try:
                route = self._fetch_driving_taxi(origin, dest, city=city)
            except Exception as exc2:
                logger.warning("Amap driving miss (%s); formula taxi", exc2)
                route = taxi_estimate(origin, dest, country="CN")
        cache_file.write_text(route.model_dump_json(), encoding="utf-8")
        return route

    def _fetch_transit(self, origin: Coord, dest: Coord, *, city: str) -> TransitRoute:
        city_code = amap_city_code(city, origin.lat, origin.lng)
        resp = httpx.get(
            AMAP_TRANSIT,
            params={
                "key": self._api_key,
                "origin": f"{origin.lng:.6f},{origin.lat:.6f}",
                "destination": f"{dest.lng:.6f},{dest.lat:.6f}",
                "city": city_code,
                "cityd": city_code,
                "output": "json",
                "strategy": 0,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("status")) != "1":
            raise RuntimeError(f"Amap transit status={data.get('status')} info={data.get('info')}")
        route_block = data.get("route") or {}
        transits = route_block.get("transits") or []
        if not transits:
            taxi_cny = _num(route_block.get("taxi_cost"))
            raise RuntimeError(f"Amap returned no transits (taxi_cost={taxi_cny})")

        chosen = min(
            transits,
            key=lambda t: (int(_num(t.get("duration")) or 10**9), float(_num(t.get("cost")) or 0)),
        )
        duration_min = max(1, round((_num(chosen.get("duration")) or 60) / 60))
        cost_usd = to_usd(_num(chosen.get("cost")) or 0, "CNY") or 0.0
        legs = _parse_amap_segments(chosen.get("segments") or [])
        if not legs:
            legs = [TransitLeg(travel_mode="transit", duration_min=duration_min, cost=cost_usd)]
        return TransitRoute(
            total_duration_min=duration_min,
            total_cost=cost_usd,
            currency="USD",
            transfer_count=max(0, len([leg for leg in legs if leg.travel_mode == "transit"]) - 1),
            legs=legs,
            data_source="amap",
        )

    def _fetch_driving_taxi(self, origin: Coord, dest: Coord, *, city: str) -> TransitRoute:
        resp = httpx.get(
            AMAP_DRIVING,
            params={
                "key": self._api_key,
                "origin": f"{origin.lng:.6f},{origin.lat:.6f}",
                "destination": f"{dest.lng:.6f},{dest.lat:.6f}",
                "output": "json",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("status")) != "1":
            raise RuntimeError(f"Amap driving status={data.get('status')} info={data.get('info')}")
        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            raise RuntimeError("Amap driving returned no paths")
        path = paths[0]
        meters = _num(path.get("distance")) or 0
        seconds = _num(path.get("duration")) or 0
        duration_min = max(5, round(seconds / 60)) if seconds else max(5, round(meters / 1000 / 22 * 60) + 3)
        km = meters / 1000
        # Amap taxi_cost is on the *transit* payload; driving has no fare.
        return taxi_estimate(origin, dest, country="CN", duration_min=duration_min, road_km=km)


def _num(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_amap_segments(segments: list) -> list[TransitLeg]:
    legs: list[TransitLeg] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        walking = seg.get("walking") or {}
        wdur = _num(walking.get("duration"))
        if wdur:
            legs.append(TransitLeg(
                travel_mode="walking",
                duration_min=max(1, round(wdur / 60)),
            ))
        bus = seg.get("bus") or {}
        for line in bus.get("buslines") or []:
            if not isinstance(line, dict):
                continue
            dep = line.get("departure_stop") or {}
            arr = line.get("arrival_stop") or {}
            legs.append(TransitLeg(
                travel_mode="transit",
                line_name=str(line.get("name") or ""),
                from_stop=str(dep.get("name") or ""),
                to_stop=str(arr.get("name") or ""),
                duration_min=max(1, round(_num(line.get("duration")) / 60)) if _num(line.get("duration")) else 1,
            ))
        railway = seg.get("railway") or {}
        if railway.get("name"):
            legs.append(TransitLeg(
                travel_mode="transit",
                line_name=str(railway.get("name") or ""),
                from_stop=str((railway.get("departure_stop") or {}).get("name") or ""),
                to_stop=str((railway.get("arrival_stop") or {}).get("name") or ""),
            ))
    return legs
