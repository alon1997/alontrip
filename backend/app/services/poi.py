"""POI data layer.

Three implementations behind one interface (D-010, T-008):

- :class:`MysqlPoiProvider` — reads the `pois` table in `hackathontrip`.
  Selected only when MySQL connectivity is confirmed at startup.
- :class:`SerpApiPoiProvider` — live ``google_maps`` engine searches with a
  file cache per city.
- :class:`LocalPoiProvider` — reads seed JSON from ``data/pois/``, no
  network, no keys.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import BaseModel

from ..config import Settings
from .currency import DISPLAY_CURRENCY, to_usd
from .db import get_connection

logger = logging.getLogger(__name__)

# SerpApi search query per city (English, for stable results).
CITY_QUERIES = {
    "tokyo": "top attractions Tokyo Japan",
    "seoul": "top attractions Seoul South Korea",
    "kyoto": "top attractions Kyoto Japan",
    "osaka": "top attractions Osaka Japan",
    "shanghai": "top attractions Shanghai China",
    "beijing": "top attractions Beijing China",
    "hongkong": "top attractions Hong Kong",
    "busan": "top attractions Busan South Korea",
    "jeju": "top attractions Jeju South Korea",
}


class Poi(BaseModel):
    id: str
    name: str
    name_en: str
    city: str
    lat: float
    lng: float
    rating: float
    suggested_duration_min: int
    category: str
    area: str | None = None
    name_local: str | None = None  # T-009: search matches against this too
    ticket_price: float | None = None
    ticket_currency: str | None = None
    requires_ticket: str | None = None  # yes / no
    opening_hours: str | None = None


def _tickets_in_usd(poi: Poi) -> Poi:
    """Catalog may still hold JPY/CNY/KRW/HKD until the next MySQL sync."""
    usd = to_usd(poi.ticket_price, poi.ticket_currency)
    poi.ticket_price = usd
    poi.ticket_currency = DISPLAY_CURRENCY if usd is not None else None
    return poi


class PoiProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def get_pois(self, city: str) -> list[Poi]:
        ...


class LocalPoiProvider(PoiProvider):
    name = "local-json"

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def get_pois(self, city: str) -> list[Poi]:
        path = self._data_dir / f"{city}.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cannot read POI seed file %s: %s", path, exc)
            return []
        # Seed files predate the `city` field (T-008): the filename is the
        # source of truth, so it always wins over anything in the JSON.
        return [_tickets_in_usd(Poi(**{**item, "city": city})) for item in raw]


class MysqlPoiProvider(PoiProvider):
    """Reads `hackathontrip.pois`. External ids are slugs, never the raw
    auto-increment primary key (T-008)."""

    name = "mysql"

    def __init__(self, settings: Settings):
        self._settings = settings

    def get_pois(self, city: str) -> list[Poi]:
        conn = get_connection(self._settings)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, slug, name, name_local, area, lat, lng, rating, "
                    "suggested_duration_min, category, ticket_price, ticket_currency, "
                    "requires_ticket, opening_hours "
                    "FROM pois WHERE city=%s",
                    (city,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        pois: list[Poi] = []
        for row in rows:
            pois.append(Poi(
                id=row["slug"] or f"p{row['id']}",
                name=row["name"],
                name_en=row["name"],
                city=city,
                lat=float(row["lat"]),
                lng=float(row["lng"]),
                rating=float(row["rating"]) if row["rating"] is not None else 0.0,
                suggested_duration_min=row["suggested_duration_min"] or 60,
                category=row["category"] or "attraction",
                area=row["area"],
                name_local=row["name_local"],
                ticket_price=float(row["ticket_price"]) if row["ticket_price"] is not None else None,
                ticket_currency=row["ticket_currency"] or None,
                requires_ticket=row["requires_ticket"] or None,
                opening_hours=row["opening_hours"] or None,
            ))
        return [_tickets_in_usd(p) for p in pois]


class SerpApiPoiProvider(PoiProvider):
    """Live POI search via SerpApi ``google_maps`` engine, cached per city."""

    name = "serpapi"

    def __init__(self, api_key: str, cache_dir: Path):
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_pois(self, city: str) -> list[Poi]:
        cache_file = self._cache_dir / f"pois_{city}.json"
        if cache_file.exists():
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
                return [_tickets_in_usd(Poi(**{**item, "city": city})) for item in raw]
            except Exception as exc:
                logger.warning("ignoring corrupt POI cache %s: %s", cache_file, exc)
        pois = self._fetch(city)
        cache_file.write_text(
            json.dumps([poi.model_dump() for poi in pois], ensure_ascii=False),
            encoding="utf-8",
        )
        return pois

    def _fetch(self, city: str) -> list[Poi]:
        query = CITY_QUERIES.get(city, f"top attractions {city}")
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_maps",
                "q": query,
                "hl": "en",
                "api_key": self._api_key,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        results = resp.json().get("local_results") or []
        pois: list[Poi] = []
        seen: set[str] = set()
        for raw in results:
            gps = raw.get("gps_coordinates") or {}
            lat, lng = gps.get("latitude"), gps.get("longitude")
            if lat is None or lng is None:
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            pois.append(_tickets_in_usd(Poi(
                id=_unique_id(_slugify(title), seen),
                name=title,
                name_en=title,
                city=city,
                lat=float(lat),
                lng=float(lng),
                rating=float(raw.get("rating") or 0.0),
                suggested_duration_min=90,
                category=str(raw.get("type") or "attraction"),
            )))
        return pois


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "poi"


def _unique_id(slug: str, seen: set[str]) -> str:
    candidate, n = slug, 2
    while candidate in seen:
        candidate = f"{slug}-{n}"
        n += 1
    seen.add(candidate)
    return candidate
