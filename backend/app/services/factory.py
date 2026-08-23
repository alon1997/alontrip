"""Provider factory (D-010, extended T-008): SerpApi/DeepSeek/MySQL
implementations when reachable, local JSON / rule-based fallbacks otherwise.

Selection happens once per process (lru_cache) and is logged, so the startup
output shows exactly which data sources are active. Each of the three keys
(SerpApi, DeepSeek, MySQL) is judged independently — missing or unreachable
one must never block another.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from . import db
from .grouping import Grouper, LLMGrouper, RuleBasedGrouper
from .lodging import LocalLodgingProvider, LodgingProvider, MysqlLodgingProvider
from .poi import LocalPoiProvider, MysqlPoiProvider, PoiProvider
from .amap import AmapTransitProvider
from .transit import (
    Coord,
    LocalTransitProvider,
    SerpApiTransitProvider,
    TransitProvider,
    TransitRoute,
    is_mainland_china,
    taxi_estimate,
)
from .transport_hub import LocalTransportHubProvider, TransportHubProvider

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]  # 04app/
POIS_DIR = REPO_ROOT / "data" / "pois"
LODGINGS_DIR = REPO_ROOT / "data" / "lodgings"
TRANSPORT_HUBS_DIR = REPO_ROOT / "data" / "transport_hubs"
SERPAPI_CACHE_DIR = REPO_ROOT / "cache"  # gitignored: raw POI search responses

# Committed to git on purpose: SerpApi writes routes here and the offline
# provider reads the same files, so one paid online run becomes the offline
# dataset and judges can run the project without an API key of their own.
TRANSIT_CACHE_DIR = REPO_ROOT / "data" / "transit_cache"


def _has_key(value: str | None) -> bool:
    return bool(value and value.strip())


@lru_cache
def _mysql_available() -> bool:
    """Probed once per process. False for empty password, unreachable host,
    or bad credentials — all treated the same as "no database configured"
    (D-010). A wrong database name is a config bug and raises instead."""
    settings = get_settings()
    return db.can_connect(settings)


@lru_cache
def get_poi_provider() -> PoiProvider:
    """Catalog reads are mysql-or-local-json only (T-008): SerpApi is a
    bulk "top attractions" pull, appropriate for one-time seeding
    (scripts/seed_pois.py) or the T-009 search endpoints, but not something
    GET /pois should trigger on every request just because MySQL happens to
    be unreachable from this machine.
    """
    settings = get_settings()
    if _mysql_available():
        logger.info("POI provider: mysql (hackathontrip.pois)")
        return MysqlPoiProvider(settings)
    logger.info("POI provider: local-json (data/pois, no MySQL — offline mode)")
    return LocalPoiProvider(data_dir=POIS_DIR)


@lru_cache
def get_lodging_provider() -> LodgingProvider:
    settings = get_settings()
    if _mysql_available():
        logger.info("Lodging provider: mysql (hackathontrip.lodgings)")
        return MysqlLodgingProvider(settings)
    logger.info("Lodging provider: local-json (data/lodgings, no MySQL — offline mode)")
    return LocalLodgingProvider(data_dir=LODGINGS_DIR)


@lru_cache
def get_transport_hub_provider() -> TransportHubProvider:
    """No MySQL-backed implementation exists yet (see transport_hub.py's
    module docstring) — this is always local-json for now."""
    logger.info("Transport hub provider: local-json (data/transport_hubs)")
    return LocalTransportHubProvider(data_dir=TRANSPORT_HUBS_DIR)


class CompositeTransitProvider(TransitProvider):
    """Japan / Korea / Hong Kong → SerpApi (Google). Mainland China → Amap
    when ``AMAP_KEY`` is set, otherwise a labelled taxi estimate (never
    Google transit — it comes back empty and burns quota).
    """

    def __init__(self, overseas: TransitProvider, amap: AmapTransitProvider | None):
        self._overseas = overseas
        self._amap = amap
        self.name = "amap+serpapi" if amap is not None else overseas.name

    def get_route(
        self,
        origin: Coord,
        dest: Coord,
        *,
        depart_at: int | None = None,
        hour_bucket: str = "",
        city: str = "",
    ) -> TransitRoute:
        if is_mainland_china(origin.lat, origin.lng, city):
            if self._amap is not None:
                return self._amap.get_route(
                    origin, dest, depart_at=depart_at, hour_bucket=hour_bucket, city=city,
                )
            logger.info("Mainland China transit: no AMAP_KEY, taxi estimate (not SerpApi)")
            return taxi_estimate(origin, dest, country="CN")
        return self._overseas.get_route(
            origin, dest, depart_at=depart_at, hour_bucket=hour_bucket, city=city,
        )


@lru_cache
def get_transit_provider() -> TransitProvider:
    settings = get_settings()
    if _has_key(settings.serpapi_key):
        overseas: TransitProvider = SerpApiTransitProvider(
            api_key=settings.serpapi_key, cache_dir=TRANSIT_CACHE_DIR,
        )
        overseas_name = "serpapi"
    else:
        overseas = LocalTransitProvider(cache_dir=TRANSIT_CACHE_DIR)
        overseas_name = "local-json"
    amap = None
    if _has_key(settings.amap_key):
        amap = AmapTransitProvider(api_key=settings.amap_key, cache_dir=TRANSIT_CACHE_DIR)
        logger.info("Transit provider: %s + amap (mainland China)", overseas_name)
    else:
        logger.info("Transit provider: %s (no AMAP_KEY; China legs use taxi estimate)", overseas_name)
    return CompositeTransitProvider(overseas, amap)


@lru_cache
def get_grouper() -> Grouper:
    settings = get_settings()
    rules = RuleBasedGrouper()
    if _has_key(settings.deepseek_api_key):
        logger.info("Grouper: deepseek (LLM, silently falls back to rule-based)")
        return LLMGrouper(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            rules=rules,
        )
    logger.info("Grouper: rule-based (geographic clustering, no DEEPSEEK_API_KEY)")
    return rules
