"""Arrival/departure transport hub data layer (T-016).

Same shape as poi.py / lodging.py (D-010): an :class:`ABC` provider with a
local-JSON implementation reading `data/transport_hubs/{city}.json`. Every
supported city gets its major airports and/or intercity train/metro
stations — travellers pick one hub to arrive at and one to leave from, and
`plan_trip` (services/planner.py) anchors day 1's start and the last day's
end there instead of a hotel.

Unlike lodgings there's no MySQL-backed provider yet: this is new data with
no existing table, and this task is local-only (no deploy) — the local-JSON
layer here already *is* the equivalent of "writing to the database" for the
demo, exactly like the pre-T-008 POI seed once was. A `MysqlTransportHubProvider`
can be added later the same way `MysqlLodgingProvider` was, without touching
this file's public shape.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

VALID_KINDS = {"airport", "station"}


class TransportHub(BaseModel):
    id: str
    name: str
    name_en: str
    city: str
    kind: str  # "airport" | "station"
    lat: float
    lng: float


class TransportHubProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def get_hubs(self, city: str) -> list[TransportHub]:
        """Return every hub (airports + stations) for the given city."""


class LocalTransportHubProvider(TransportHubProvider):
    name = "local-json"

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def get_hubs(self, city: str) -> list[TransportHub]:
        path = self._data_dir / f"{city}.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cannot read transport hub seed file %s: %s", path, exc)
            return []
        return [TransportHub(**{**item, "city": city}) for item in raw]
