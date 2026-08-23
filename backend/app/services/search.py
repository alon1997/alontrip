"""POI / lodging search (T-009).

Search-before-SerpApi discipline (实现批次.md 2.5): always check the current
catalog provider (mysql or local-json) first, case-insensitively, against
id/name/name_en/name_local. Only call SerpApi's `google_maps` engine on a
genuine miss, and only if `SERPAPI_KEY` is configured — a miss with no key
is a 200 with empty results, never a 500.

A SerpApi hit is written back to MySQL (source="search") so the next search
for the same place is a local hit, not another billed call. This never
touches the local JSON seed files — those are only ever written by
scripts/seed_pois.py.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings
from .db import get_connection
from .factory import get_lodging_provider, get_poi_provider
from .lodging import Lodging, LodgingProvider, MysqlLodgingProvider
from .poi import Poi, PoiProvider, MysqlPoiProvider, _slugify, _unique_id

logger = logging.getLogger(__name__)

SERPAPI_TIMEOUT = 15.0


def _text_matches(query: str, *fields: str | None) -> bool:
    q = query.lower()
    return any(q in (field or "").lower() for field in fields)


def _mapped_source(provider_name: str) -> str:
    """`GET /pois` and `GET /lodgings/search` speak of the "current
    provider" as mysql|local-json; the search response contract (2.5)
    calls the mysql case "db" instead."""
    return "db" if provider_name == "mysql" else provider_name


# ---------------------------------------------------------------- POIs ----

def search_pois(city: str, query: str, settings: Settings) -> tuple[str, list[Poi]]:
    provider: PoiProvider = get_poi_provider()
    hits = [
        poi for poi in provider.get_pois(city)
        if _text_matches(query, poi.id, poi.name, poi.name_en, poi.name_local)
    ]
    if hits:
        return _mapped_source(provider.name), hits

    if not settings.serpapi_key:
        return _mapped_source(provider.name), []

    results = _serpapi_search_pois(city, query, settings.serpapi_key)
    if results and isinstance(provider, MysqlPoiProvider):
        _insert_search_pois(settings, city, results)
    return "serpapi", [poi for poi, _place_id in results]


def _serpapi_search_pois(city: str, query: str, api_key: str) -> list[tuple[Poi, str | None]]:
    try:
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"engine": "google_maps", "q": f"{query} {city}", "hl": "en", "api_key": api_key},
            timeout=SERPAPI_TIMEOUT,
        )
        resp.raise_for_status()
        raw_results = resp.json().get("local_results") or []
    except Exception as exc:  # noqa: BLE001 — SerpApi failure must not 500 the search
        logger.warning("SerpApi POI search failed for %r/%r: %s", city, query, exc)
        return []

    out: list[tuple[Poi, str | None]] = []
    seen: set[str] = set()
    for raw in raw_results:
        gps = raw.get("gps_coordinates") or {}
        lat, lng = gps.get("latitude"), gps.get("longitude")
        if lat is None or lng is None:
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        poi = Poi(
            id=_unique_id(_slugify(title), seen),
            name=title,
            name_en=title,
            city=city,
            lat=float(lat),
            lng=float(lng),
            rating=float(raw.get("rating") or 0.0),
            suggested_duration_min=90,
            category=str(raw.get("type") or "attraction"),
        )
        out.append((poi, raw.get("place_id")))
    return out


def _insert_search_pois(settings: Settings, city: str, results: list[tuple[Poi, str | None]]) -> None:
    conn = get_connection(settings)
    try:
        with conn.cursor() as cur:
            for poi, place_id in results:
                cur.execute(
                    "INSERT INTO pois (slug, name, city, area, lat, lng, rating, "
                    "suggested_duration_min, category, source, place_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'search', %s) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), rating=VALUES(rating)",
                    (poi.id, poi.name, city, poi.area, poi.lat, poi.lng, poi.rating,
                     poi.suggested_duration_min, poi.category, place_id),
                )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort; results already returned
        logger.warning("failed to persist search-added POIs for %r: %s", city, exc)
    finally:
        conn.close()


# ------------------------------------------------------------- Lodgings ----

def search_lodgings(city: str, query: str, settings: Settings) -> tuple[str, list[Lodging]]:
    provider: LodgingProvider = get_lodging_provider()
    catalog = (
        provider.get_all_lodgings(city)
        if isinstance(provider, MysqlLodgingProvider)
        else provider.get_lodgings(city)
    )
    hits = [
        lodging for lodging in catalog
        if _text_matches(query, lodging.id, lodging.name, lodging.name_local)
    ]
    if hits:
        return _mapped_source(provider.name), hits

    if not settings.serpapi_key:
        return _mapped_source(provider.name), []

    results = _serpapi_search_lodgings(city, query, settings.serpapi_key)
    if results and isinstance(provider, MysqlLodgingProvider):
        _insert_search_lodgings(settings, city, results)
    return "serpapi", [lodging for lodging, _place_id in results]


def _serpapi_search_lodgings(city: str, query: str, api_key: str) -> list[tuple[Lodging, str | None]]:
    try:
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"engine": "google_maps", "q": f"{query} {city} hotel", "hl": "en", "api_key": api_key},
            timeout=SERPAPI_TIMEOUT,
        )
        resp.raise_for_status()
        raw_results = resp.json().get("local_results") or []
    except Exception as exc:  # noqa: BLE001 — SerpApi failure must not 500 the search
        logger.warning("SerpApi lodging search failed for %r/%r: %s", city, query, exc)
        return []

    out: list[tuple[Lodging, str | None]] = []
    seen: set[str] = set()
    for raw in raw_results:
        gps = raw.get("gps_coordinates") or {}
        lat, lng = gps.get("latitude"), gps.get("longitude")
        if lat is None or lng is None:
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        rate = ((raw.get("rate_per_night") or {}).get("extracted_lowest"))
        lodging = Lodging(
            id=_unique_id(_slugify(title), seen),
            name=title,
            city=city,
            lat=float(lat),
            lng=float(lng),
            kind="hotel",
            listed=0,  # search-added, never joins the default catalog (T-008)
            price_per_night=float(rate) if rate is not None else None,
            rating=float(raw.get("rating") or 0.0) or None,
        )
        out.append((lodging, raw.get("place_id")))
    return out


def _insert_search_lodgings(settings: Settings, city: str, results: list[tuple[Lodging, str | None]]) -> None:
    conn = get_connection(settings)
    try:
        with conn.cursor() as cur:
            for lodging, place_id in results:
                cur.execute(
                    "INSERT INTO lodgings (slug, name, city, area, lat, lng, kind, listed, "
                    "price_per_night, rating, source, place_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'search', %s) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), rating=VALUES(rating)",
                    (lodging.id, lodging.name, city, lodging.area, lodging.lat, lodging.lng,
                     lodging.kind, lodging.price_per_night, lodging.rating, place_id),
                )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort; results already returned
        logger.warning("failed to persist search-added lodgings for %r: %s", city, exc)
    finally:
        conn.close()
