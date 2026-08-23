"""Pre-fetch the transit matrix for a city into ``data/transit_cache/``.

Run this once with a SerpApi key; the cached responses are committed to the
repository, so the app serves real transit data with no key configured and
anyone cloning the project can run it as-is.

    python scripts/fetch_transit.py --city tokyo --demo
    python scripts/fetch_transit.py --city tokyo            # every POI pair
    python scripts/fetch_transit.py --city tokyo --dry-run  # cost estimate only

Existing cache files are never re-fetched, so re-running after a failure only
pays for the pairs that are still missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.transit import (  # noqa: E402
    Coord,
    SerpApiTransitProvider,
    _route_cache_path,
)

CACHE_DIR = REPO_ROOT / "data" / "transit_cache"
POIS_DIR = REPO_ROOT / "data" / "pois"
ENV_FILE = REPO_ROOT / ".env"

# The seven stops in the demo walkthrough. They cover all three days of the
# Tokyo itinerary and spread across the city, so the day-coloured routes are
# visually distinct on the map.
DEMO_POIS = {
    "tokyo": [
        "senso-ji",         # day 1 — north-east
        "akihabara",        # day 1 — centre-north
        "shinjuku",         # day 1 — west
        "shibuya-crossing", # day 2 — south-west
        "harajuku",         # day 2 — west-centre
        "roppongi-hills",   # day 2 — south-centre
        "ginza",            # day 3 — south-east
    ]
}


def read_api_key() -> str:
    if not ENV_FILE.exists():
        raise SystemExit(f"missing {ENV_FILE} — copy .env.example and add your key")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("SERPAPI_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise SystemExit("SERPAPI_KEY is empty in .env")


def load_pois(city: str, demo_only: bool) -> list[dict]:
    path = POIS_DIR / f"{city}.json"
    if not path.exists():
        raise SystemExit(f"no POI data for {city!r} at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    pois = raw["pois"] if isinstance(raw, dict) else raw
    if demo_only:
        wanted = DEMO_POIS.get(city)
        if not wanted:
            raise SystemExit(f"no demo subset defined for {city!r}")
        by_id = {p["id"]: p for p in pois}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise SystemExit(f"demo POIs missing from {path.name}: {missing}")
        return [by_id[i] for i in wanted]
    return pois


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="tokyo")
    parser.add_argument("--demo", action="store_true", help="only the demo subset")
    parser.add_argument("--dry-run", action="store_true", help="report cost, fetch nothing")
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between calls")
    args = parser.parse_args()

    pois = load_pois(args.city, args.demo)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    pairs = [(a, b) for a in pois for b in pois if a["id"] != b["id"]]
    todo = []
    for origin, dest in pairs:
        o = Coord(lat=origin["lat"], lng=origin["lng"])
        d = Coord(lat=dest["lat"], lng=dest["lng"])
        if not _route_cache_path(CACHE_DIR, o, d).exists():
            todo.append((origin, dest, o, d))

    cached = len(pairs) - len(todo)
    print(f"{args.city}: {len(pois)} POIs, {len(pairs)} directed pairs")
    print(f"  already cached : {cached}")
    print(f"  to fetch       : {len(todo)}  (1 SerpApi credit each)")

    if args.dry_run or not todo:
        return

    provider = SerpApiTransitProvider(api_key=read_api_key(), cache_dir=CACHE_DIR)
    failures: list[tuple[str, str, str]] = []

    for i, (origin, dest, o, d) in enumerate(todo, 1):
        label = f"{origin['id']} -> {dest['id']}"
        try:
            route = provider.get_route(o, d)
            lines = " + ".join(
                leg.line_name for leg in route.legs if leg.travel_mode == "transit"
            )
            print(
                f"  [{i:>3}/{len(todo)}] {label:<42} "
                f"{route.total_duration_min:>3} min  "
                f"{int(route.total_cost):>4} {route.currency}  "
                f"{route.transfer_count} transfer(s)  {lines[:48]}"
            )
        except Exception as exc:  # noqa: BLE001 — one bad pair must not stop the batch
            failures.append((origin["id"], dest["id"], str(exc)))
            print(f"  [{i:>3}/{len(todo)}] {label:<42} FAILED: {exc}")
        time.sleep(args.delay)

    print(f"\nfetched {len(todo) - len(failures)}/{len(todo)}; cache now at {CACHE_DIR}")
    if failures:
        print(f"{len(failures)} failed — re-run to retry only those:")
        for a, b, err in failures:
            print(f"  {a} -> {b}: {err}")


if __name__ == "__main__":
    main()
