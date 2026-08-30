"""Daily grouping algorithms (D-010).

Two implementations behind one interface:

- :class:`RuleBasedGrouper` — deterministic, zero dependencies: k-means
  geographic clustering (farthest-first init) followed by nearest-neighbor
  greedy ordering within each day.
- :class:`LLMGrouper` — DeepSeek API via the Anthropic-compatible endpoint.
  Any failure (missing key, HTTP error, malformed reply) silently falls back
  to the rule-based implementation — never an exception to the frontend.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from .poi import Poi
from .transit import TransitRoute, haversine_km

logger = logging.getLogger(__name__)

DISTRICT_MERGE_KM = 1.2
# Official list: deepseek-v4-flash / deepseek-v4-pro
# (https://api-docs.deepseek.com/zh-cn/). Product grouping uses flash:
# cheaper and faster; a timeout still falls back to the rule-based grouper.
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_MAX_TOKENS = 4096
DEEPSEEK_TIMEOUT_S = 45.0


def deepseek_messages_body(prompt: str, *, max_tokens: int = DEEPSEEK_MAX_TOKENS) -> dict:
    """Anthropic-compatible /v1/messages body.

    v4-flash thinking is ON by default and counts toward max_tokens. A city
    fill with a long hotel list can return HTTP 200, stop_reason=end_turn or
    max_tokens, and only a ``thinking`` block — then ``_find_text_block``
    raises and the whole city falls back to Python. Itinerary JSON does not
    need a chain of thought, so thinking is off (D-018).
    """
    return {
        "model": DEEPSEEK_MODEL,
        "max_tokens": max_tokens,
        # Structured extraction (copying ids, grouping by day) needs no
        # creativity. 2026-08-28 prod: at the default high temperature the same
        # 17-spot prompt failed three different ways across three runs (wrong
        # id / near-miss id / non-JSON).
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }


def _content_block_types(content: object) -> list[str]:
    if not isinstance(content, list):
        return [type(content).__name__]
    return [str(block.get("type")) for block in content if isinstance(block, dict)]


def _find_text_block(content: list[dict]) -> str:
    """DeepSeek's reasoning model emits a ``thinking`` block before the
    actual reply, so ``content[0]`` is not reliably the text (found in T-007,
    fixed here and in planner.py's own DeepSeek call — see dev log T-007)."""
    for block in content:
        if block.get("type") == "text":
            return block.get("text", "")
    types = _content_block_types(content)
    raise ValueError(f"no text block in DeepSeek reply (blocks={types})")


class RouteSegment(BaseModel):
    from_poi_id: str
    to_poi_id: str
    route: TransitRoute


class DayItinerary(BaseModel):
    day: int
    poi_ids: list[str]
    segments: list[RouteSegment]
    transit_minutes: int = 0
    transit_cost: float = 0.0
    currency: str = "JPY"
    transfer_count: int = 0
    # False when any leg of the day fell back to a distance estimate, so the
    # UI can label the day's totals instead of passing guesses off as data.
    all_real_data: bool = True


def _centroid(group: list[Poi]) -> tuple[float, float]:
    return (
        sum(p.lat for p in group) / len(group),
        sum(p.lng for p in group) / len(group),
    )


def _centroid_distance(a: list[Poi], b: list[Poi]) -> float:
    return haversine_km(*_centroid(a), *_centroid(b))


def _point_centroid_distance(p: Poi, group: list[Poi]) -> float:
    return haversine_km(p.lat, p.lng, *_centroid(group))


class Grouper(ABC):
    name: str = "unknown"

    @abstractmethod
    def group(self, pois: list[Poi], days: int) -> list[list[Poi]]:
        ...


class RuleBasedGrouper(Grouper):
    """Geographic clustering + nearest-neighbor greedy, no external calls."""

    name = "rule-based"

    def group(self, pois: list[Poi], days: int) -> list[list[Poi]]:
        if not pois:
            return []
        k = max(1, min(days, len(pois)))
        clusters = [group for group in self._cluster(pois, k) if group]
        clusters = self._merge_close_clusters(clusters)
        # Fit to day count by merging nearest districts / splitting the
        # largest. Do not call _rebalance (that evened counts and split
        # the Bund / Lujiazui apart). Shanghai check: the-bund + lujiazui + shanghai-tower
        # stay one day when grouped with yu-garden/xintiandi into 3 days.
        clusters = self._fit_to_day_count(clusters, k)
        ordered = [self._order_cluster(cluster) if cluster else [] for cluster in clusters]
        ordered.sort(
            key=lambda group: (sum(p.lat for p in group) / len(group)) if group else -90.0,
            reverse=True,
        )
        return ordered

    def _cluster(self, pois: list[Poi], k: int) -> list[list[Poi]]:
        centers: list[tuple[float, float]] = [(pois[0].lat, pois[0].lng)]
        # Farthest-first initialization — deterministic, no randomness.
        while len(centers) < k:
            farthest = max(
                pois,
                key=lambda p: min(
                    haversine_km(p.lat, p.lng, clat, clng) for clat, clng in centers
                ),
            )
            centers.append((farthest.lat, farthest.lng))
        for _ in range(50):
            labels = [
                min(range(k), key=lambda j: haversine_km(p.lat, p.lng, *centers[j]))
                for p in pois
            ]
            updated = []
            for j in range(k):
                members = [pois[i] for i, label in enumerate(labels) if label == j]
                if members:
                    updated.append((
                        sum(p.lat for p in members) / len(members),
                        sum(p.lng for p in members) / len(members),
                    ))
                else:
                    updated.append(centers[j])
            if updated == centers:
                break
            centers = updated
        labels = [
            min(range(k), key=lambda j: haversine_km(p.lat, p.lng, *centers[j]))
            for p in pois
        ]
        groups = [
            [pois[i] for i, label in enumerate(labels) if label == j] for j in range(k)
        ]
        # Repair empty clusters: steal the farthest member of the largest group.
        for j in range(k):
            if groups[j]:
                continue
            largest = max(range(k), key=lambda idx: len(groups[idx]))
            if len(groups[largest]) > 1:
                farthest = max(
                    groups[largest],
                    key=lambda p: haversine_km(p.lat, p.lng, *centers[j]),
                )
                groups[largest].remove(farthest)
                groups[j].append(farthest)
        return [group for group in groups if group]

    def _merge_close_clusters(self, groups: list[list[Poi]]) -> list[list[Poi]]:
        """Merge districts whose centroids are closer than DISTRICT_MERGE_KM."""
        groups = [list(group) for group in groups if group]
        changed = True
        while changed and len(groups) > 1:
            changed = False
            best_pair: tuple[int, int] | None = None
            best_d = DISTRICT_MERGE_KM
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    dist = _centroid_distance(groups[i], groups[j])
                    if dist < best_d:
                        best_d = dist
                        best_pair = (i, j)
            if best_pair is not None:
                i, j = best_pair
                groups[i] = groups[i] + groups[j]
                groups.pop(j)
                changed = True
        return groups

    def _fit_to_day_count(self, groups: list[list[Poi]], days: int) -> list[list[Poi]]:
        """End with exactly ``days`` groups: merge closest extras, split oversized."""
        groups = [list(group) for group in groups if group]
        while len(groups) > days and len(groups) >= 2:
            best_pair = (0, 1)
            best_d = _centroid_distance(groups[0], groups[1])
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    dist = _centroid_distance(groups[i], groups[j])
                    if dist < best_d:
                        best_d = dist
                        best_pair = (i, j)
            i, j = best_pair
            groups[i] = groups[i] + groups[j]
            groups.pop(j)
        while len(groups) < days:
            largest = max(groups, key=len) if groups else []
            if len(largest) < 2:
                groups.append([])
                continue
            clat, clng = _centroid(largest)
            farthest = max(largest, key=lambda p: haversine_km(p.lat, p.lng, clat, clng))
            largest.remove(farthest)
            groups.append([farthest])
        while len(groups) < days:
            groups.append([])
        return groups[:days]

    def _rebalance(self, groups: list[list[Poi]], n: int) -> list[list[Poi]]:
        """Unused leftover: count-balancing split districts. Do not call from group().

        Capacity-balanced: any two days differ by at most 1 spot (lands in
        the n//k ~ ceil(n/k) range).
        """
        k = len(groups)
        if k <= 1:
            return groups
        target_low, target_high = n // k, -(-n // k)
        for _ in range(n):  # each move strictly decreases the imbalance; converges within n steps
            overloaded = [g for g in groups if len(g) > target_high]
            underloaded = [g for g in groups if len(g) < target_low]
            if not overloaded or not underloaded:
                break
            source = max(overloaded, key=len)
            target = min(underloaded, key=lambda g: _centroid_distance(source, g))
            mover = min(source, key=lambda p: _point_centroid_distance(p, target))
            source.remove(mover)
            target.append(mover)
        return groups

    def _order_cluster(self, cluster: list[Poi]) -> list[Poi]:
        centroid = (
            sum(p.lat for p in cluster) / len(cluster),
            sum(p.lng for p in cluster) / len(cluster),
        )
        current = min(cluster, key=lambda p: haversine_km(p.lat, p.lng, *centroid))
        remaining = list(cluster)
        ordered: list[Poi] = []
        while remaining:
            remaining.remove(current)
            ordered.append(current)
            if remaining:
                current = min(
                    remaining,
                    key=lambda p: haversine_km(current.lat, current.lng, p.lat, p.lng),
                )
        return ordered


class LLMGrouper(Grouper):
    """DeepSeek-powered grouping with a silent, reliable rule-based fallback.

    ``name`` reports whichever implementation actually ran last: deepseek on
    an LLM success, rule-based (deepseek unavailable) after a fallback —
    never lie to the caller.
    """

    def __init__(self, api_key: str, base_url: str, rules: Grouper):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._rules = rules
        self._effective_name = "deepseek"  # configured, no call made yet

    @property
    def name(self) -> str:
        return self._effective_name

    def group(self, pois: list[Poi], days: int) -> list[list[Poi]]:
        try:
            groups = self._call_llm(pois, days)
            if groups is not None:
                self._effective_name = "deepseek"
                return groups
        except Exception as exc:  # noqa: BLE001 — silent fallback is a requirement
            logger.warning("LLM grouping unavailable (%s); falling back to rule-based", exc)
        self._effective_name = "rule-based (deepseek unavailable)"
        return self._rules.group(pois, days)

    def _call_llm(self, pois: list[Poi], days: int) -> list[list[Poi]] | None:
        prompt = (
            "You are a travel route optimizer. Group the following tourist spots into "
            f"exactly {days} day-by-day itineraries so that each day covers spots that "
            "are geographically close to each other, and the order inside each day "
            "makes a sensible path. Reply with ONLY a JSON object of the form "
            '{"days": [["poi_id", ...], ...]} using every id exactly once.\n\n'
            + "\n".join(f"- {p.id}: {p.name_en} ({p.lat:.4f},{p.lng:.4f})" for p in pois)
        )
        resp = httpx.post(
            f"{self._base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=deepseek_messages_body(prompt),
            timeout=DEEPSEEK_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        logger.info(
            "DeepSeek group stop=%s blocks=%s usage=%s",
            payload.get("stop_reason"),
            _content_block_types(payload.get("content")),
            payload.get("usage"),
        )
        text = _find_text_block(payload["content"])
        parsed = _extract_json(text)
        raw_days = parsed.get("days")
        if not isinstance(raw_days, list) or len(raw_days) != days:
            logger.warning("LLM returned malformed grouping; falling back to rule-based")
            return None
        by_id = {p.id: p for p in pois}
        flat = [poi_id for group in raw_days for poi_id in group]
        if len(flat) != len(by_id) or set(flat) != set(by_id):
            logger.warning("LLM grouping misses/duplicates ids; falling back to rule-based")
            return None
        groups = [[by_id[poi_id] for poi_id in group] for group in raw_days if group]
        if len(groups) != days:
            logger.warning("LLM grouping lost a day; falling back to rule-based")
            return None
        return groups


def _extract_json(text: str) -> dict:
    """Best-effort JSON object extraction from an LLM reply."""
    text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in LLM reply") from None
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("LLM reply is not a JSON object")
    return obj
