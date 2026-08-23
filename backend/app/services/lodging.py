"""Lodging (hostel/capsule/hotel) data layer (T-008).

Same two-tier shape as poi.py (D-010):

- :class:`MysqlLodgingProvider` — reads `hackathontrip.lodgings`, only
  `listed=1` rows for the default catalog.
- :class:`LocalLodgingProvider` — reads seed JSON from `data/lodgings/`,
  where every entry is already `listed=1` (search-added hotels are a T-009
  concern and don't exist in the offline seed).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings
from .db import get_connection

logger = logging.getLogger(__name__)


class Lodging(BaseModel):
    id: str
    name: str
    city: str
    lat: float
    lng: float
    kind: str = "unknown"
    listed: int = 1
    area: str | None = None
    price_per_night: float | None = None
    price_currency: str | None = None
    rating: float | None = None
    name_local: str | None = None  # T-009: search matches against this too


class LodgingProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def get_lodgings(self, city: str) -> list[Lodging]:
        """Return the default catalog (listed=1) for the given city."""


class LocalLodgingProvider(LodgingProvider):
    name = "local-json"

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def get_lodgings(self, city: str) -> list[Lodging]:
        path = self._data_dir / f"{city}.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cannot read lodging seed file %s: %s", path, exc)
            return []
        lodgings = [Lodging(**{**item, "city": city}) for item in raw]
        return [lodging for lodging in lodgings if lodging.listed]


def _row_to_lodging(row: dict, city: str) -> Lodging:
    return Lodging(
        id=row["slug"] or f"l{row['id']}",
        name=row["name"],
        city=city,
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        kind=row["kind"] or "unknown",
        listed=int(row["listed"]),
        area=row["area"],
        price_per_night=float(row["price_per_night"]) if row["price_per_night"] is not None else None,
        price_currency=row["price_currency"],
        rating=float(row["rating"]) if row["rating"] is not None else None,
        name_local=row["name_local"],
    )


_LODGING_COLUMNS = (
    "id, slug, name, name_local, area, lat, lng, kind, listed, "
    "price_per_night, price_currency, rating"
)


class MysqlLodgingProvider(LodgingProvider):
    """Reads `hackathontrip.lodgings`. External ids are slugs (T-008)."""

    name = "mysql"

    def __init__(self, settings: Settings):
        self._settings = settings

    def get_lodgings(self, city: str) -> list[Lodging]:
        rows = self._query(city, listed_only=True)
        return [_row_to_lodging(row, city) for row in rows]

    def get_all_lodgings(self, city: str) -> list[Lodging]:
        """Includes listed=0 (search-added) rows, for T-009 search-before-
        SerpApi matching — a hotel someone already added shouldn't be
        re-fetched from SerpApi on every subsequent search."""
        rows = self._query(city, listed_only=False)
        return [_row_to_lodging(row, city) for row in rows]

    def _query(self, city: str, *, listed_only: bool) -> list[dict]:
        sql = f"SELECT {_LODGING_COLUMNS} FROM lodgings WHERE city=%s"
        params: tuple = (city,)
        if listed_only:
            sql += " AND listed=1"
        conn = get_connection(self._settings)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()
