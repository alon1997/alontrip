"""Build the 8-city POI catalog and emit SQL for hackathontrip.pois.

Fetches SerpApi google_maps with a file cache (same query is never paid twice),
merges the existing hand-written JSON (keeps demo slugs), and writes:

- data/pois/{city}.json          local fallback, D-010
- db/seed_pois.sql               INSERT for the server table

    python scripts/seed_pois.py
    python scripts/seed_pois.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.poi import _slugify, _unique_id  # noqa: E402

POIS_DIR = REPO_ROOT / "data" / "pois"
CACHE_DIR = REPO_ROOT / "data" / "poi_serpapi_cache"
SQL_PATH = REPO_ROOT / "db" / "seed_pois.sql"
ENV_FILE = REPO_ROOT / ".env"
MIN_PER_CITY = 30
MATCH_METERS = 180

CITIES = {
    "tokyo": {
        "label": "Tokyo Japan",
        "ll": "@35.6812,139.7671,12z",
        "currency": "JPY",
    },
    "kyoto": {
        "label": "Kyoto Japan",
        "ll": "@35.0116,135.7681,12z",
        "currency": "JPY",
    },
    "osaka": {
        "label": "Osaka Japan",
        "ll": "@34.6937,135.5023,12z",
        "currency": "JPY",
    },
    "seoul": {
        "label": "Seoul South Korea",
        "ll": "@37.5665,126.9780,12z",
        "currency": "KRW",
    },
    "busan": {
        "label": "Busan South Korea",
        "ll": "@35.1796,129.0756,12z",
        "currency": "KRW",
    },
    "shanghai": {
        "label": "Shanghai China",
        "ll": "@31.2304,121.4737,12z",
        "currency": "CNY",
    },
    "beijing": {
        "label": "Beijing China",
        "ll": "@39.9042,116.4074,12z",
        "currency": "CNY",
    },
    "hongkong": {
        "label": "Hong Kong",
        "ll": "@22.3193,114.1694,12z",
        "currency": "HKD",
    },
}

SKIP_TYPE = (
    "hotel",
    "lodging",
    "motel",
    "hostel",
    "inn",
    "ryokan",
    "guest house",
    "restaurant",
    "cafe",
    "coffee",
    "bar",
    "pub",
    "convenience store",
    "supermarket",
    "grocery",
    "atm",
    "bank",
    "parking",
    "gas station",
    "real estate",
    "apartment",
    "condominium",
    "travel agency",
    "car rental",
    "pharmacy",
)


def read_api_key() -> str:
    if not ENV_FILE.exists():
        raise SystemExit(f"missing {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("SERPAPI_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise SystemExit("SERPAPI_KEY is empty in .env")


def account_info(key: str) -> dict:
    resp = httpx.get(
        "https://serpapi.com/account.json",
        params={"api_key": key},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def skip_type(raw_type: str) -> bool:
    t = (raw_type or "").lower()
    return any(token in t for token in SKIP_TYPE)


def area_from_address(address: str, city: str) -> str | None:
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    skip = {city, "japan", "china", "south korea", "korea", "hong kong"}
    cleaned = [p for p in parts if p.lower() not in skip and not re.fullmatch(r"\d{3,}", p)]
    if not cleaned:
        return None
    # First segment is usually the neighbourhood / street.
    area = cleaned[0]
    return area[:80] if area else None


def hours_from(raw: dict) -> str | None:
    hours = raw.get("hours")
    if isinstance(hours, str) and hours.strip():
        return hours.strip()[:512]
    operating = raw.get("operating_hours")
    if isinstance(operating, dict) and operating:
        bits = [f"{day}: {val}" for day, val in operating.items() if val]
        text = "; ".join(bits)
        return text[:512] if text else None
    return None


def cache_path(city: str, query: str, start: int) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:80]
    return CACHE_DIR / f"{city}__{slug}__start{start}.json"


def fetch_page(key: str, query: str, ll: str, start: int, cache_file: Path) -> dict:
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    resp = httpx.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_maps",
            "type": "search",
            "q": query,
            "ll": ll,
            "hl": "en",
            "start": start,
            "api_key": key,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.35)
    return payload


def parse_results(payload: dict, city: str) -> list[dict]:
    rows = []
    for raw in payload.get("local_results") or []:
        gps = raw.get("gps_coordinates") or {}
        lat, lng = gps.get("latitude"), gps.get("longitude")
        title = str(raw.get("title") or "").strip()
        if lat is None or lng is None or not title:
            continue
        if skip_type(str(raw.get("type") or "")):
            continue
        place_id = str(raw.get("place_id") or raw.get("data_id") or "").strip() or None
        rating = raw.get("rating")
        rows.append(
            {
                "name": title,
                "lat": float(lat),
                "lng": float(lng),
                "rating": float(rating) if rating is not None else None,
                "category": str(raw.get("type") or "attraction"),
                "area": area_from_address(str(raw.get("address") or ""), city),
                "opening_hours": hours_from(raw),
                "place_id": place_id,
                "source": "seed",
            }
        )
    return rows


def queries_for(label: str) -> list[str]:
    return [
        f"tourist attractions {label}",
        f"museums parks temples {label}",
        f"landmarks {label}",
    ]


def load_handwritten(city: str) -> list[dict]:
    path = POIS_DIR / f"{city}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw["pois"] if isinstance(raw, dict) else raw
    out = []
    for item in items:
        out.append(
            {
                "slug": item["id"],
                "name": item.get("name_en") or item["name"],
                "name_local": item.get("name") if item.get("name") != item.get("name_en") else None,
                "lat": float(item["lat"]),
                "lng": float(item["lng"]),
                "rating": float(item["rating"]) if item.get("rating") is not None else None,
                "suggested_duration_min": int(item.get("suggested_duration_min") or 90),
                "category": item.get("category") or "attraction",
                "area": None,
                "opening_hours": None,
                "place_id": None,
                "source": "seed",
            }
        )
    return out


def find_match(existing: list[dict], lat: float, lng: float) -> dict | None:
    for row in existing:
        if haversine_m(row["lat"], row["lng"], lat, lng) <= MATCH_METERS:
            return row
    return None


def merge_city(city: str, fetched: list[dict], handwritten: list[dict]) -> list[dict]:
    merged = [dict(row) for row in handwritten]
    seen_places = {row["place_id"] for row in merged if row.get("place_id")}
    seen_slugs: set[str] = {row["slug"] for row in merged if row.get("slug")}

    for item in fetched:
        place_id = item.get("place_id")
        if place_id and place_id in seen_places:
            continue
        hit = find_match(merged, item["lat"], item["lng"])
        if hit:
            if place_id and not hit.get("place_id"):
                hit["place_id"] = place_id
                seen_places.add(place_id)
            if item.get("opening_hours") and not hit.get("opening_hours"):
                hit["opening_hours"] = item["opening_hours"]
            if item.get("area") and not hit.get("area"):
                hit["area"] = item["area"]
            if item.get("rating") and not hit.get("rating"):
                hit["rating"] = item["rating"]
            continue
        slug = _unique_id(_slugify(item["name"]), seen_slugs)
        merged.append(
            {
                "slug": slug,
                "name": item["name"],
                "name_local": None,
                "lat": item["lat"],
                "lng": item["lng"],
                "rating": item.get("rating"),
                "suggested_duration_min": 90,
                "category": item.get("category") or "attraction",
                "area": item.get("area"),
                "opening_hours": item.get("opening_hours"),
                "place_id": place_id,
                "source": "seed",
            }
        )
        if place_id:
            seen_places.add(place_id)
    return merged


def sql_str(value: object) -> str:
    if value is None or value == "":
        return "NULL"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def sql_num(value: object, decimals: int | None = None) -> str:
    if value is None:
        return "NULL"
    if decimals is None:
        return str(int(value))
    return f"{float(value):.{decimals}f}"


def to_json_item(row: dict) -> dict:
    item = {
        "id": row["slug"],
        "name": row.get("name_local") or row["name"],
        "name_en": row["name"],
        "lat": round(float(row["lat"]), 7),
        "lng": round(float(row["lng"]), 7),
        "rating": float(row["rating"]) if row.get("rating") is not None else 0.0,
        "suggested_duration_min": int(row.get("suggested_duration_min") or 90),
        "category": row.get("category") or "attraction",
    }
    if row.get("area"):
        item["area"] = row["area"]
    if row.get("opening_hours"):
        item["opening_hours"] = row["opening_hours"]
    return item


def write_sql(all_rows: list[tuple[str, dict]]) -> None:
    lines = [
        "-- Seed catalog for hackathontrip.pois (D-014).",
        "-- Generated by scripts/seed_pois.py. No passwords in this file.",
        "-- Ticket fields stay unknown: APIs rarely return real admission prices.",
        "SET NAMES utf8mb4;",
        "",
    ]
    for city, row in all_rows:
        lines.append(
            "INSERT INTO pois ("
            "slug, name, name_local, city, area, lat, lng, "
            "requires_ticket, ticket_price, ticket_currency, opening_hours, "
            "rating, suggested_duration_min, category, source, place_id"
            ") VALUES ("
            f"{sql_str(row['slug'])}, "
            f"{sql_str(row['name'])}, "
            f"{sql_str(row.get('name_local'))}, "
            f"{sql_str(city)}, "
            f"{sql_str(row.get('area'))}, "
            f"{sql_num(row['lat'], 7)}, "
            f"{sql_num(row['lng'], 7)}, "
            "'unknown', NULL, NULL, "
            f"{sql_str(row.get('opening_hours'))}, "
            f"{sql_num(row.get('rating'), 2) if row.get('rating') is not None else 'NULL'}, "
            f"{sql_num(row.get('suggested_duration_min'))}, "
            f"{sql_str(row.get('category'))}, "
            f"{sql_str(row.get('source') or 'seed')}, "
            f"{sql_str(row.get('place_id'))}"
            ");"
        )
    SQL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQL_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_city(key: str, city: str, meta: dict, dry_run: bool) -> list[dict]:
    collected: list[dict] = []
    seen_titles: set[str] = set()
    for query in queries_for(meta["label"]):
        if len(collected) >= MIN_PER_CITY:
            break
        for start in (0, 20):
            if len(collected) >= MIN_PER_CITY and start > 0:
                break
            cache_file = cache_path(city, query, start)
            if dry_run and not cache_file.exists():
                print(f"  would fetch {city} q={query!r} start={start}")
                continue
            payload = fetch_page(key, query, meta["ll"], start, cache_file)
            page = parse_results(payload, city)
            print(f"  {city} {query!r} start={start}: {len(page)} usable (cache={cache_file.name})")
            for row in page:
                title_key = row["name"].casefold()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                collected.append(row)
            if len(page) < 8:
                break
    return collected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = read_api_key()
    before = account_info(key)
    print(
        "serpapi credits: "
        f"{before.get('this_month_usage')} used / "
        f"{before.get('plan_searches_left')} left this plan"
    )

    all_sql_rows: list[tuple[str, dict]] = []
    for city, meta in CITIES.items():
        handwritten = load_handwritten(city)
        fetched = fetch_city(key, city, meta, args.dry_run)
        merged = merge_city(city, fetched, handwritten)
        print(f"{city}: handwritten {len(handwritten)} + api {len(fetched)} -> {len(merged)}")
        if len(merged) < MIN_PER_CITY and not args.dry_run:
            print(f"  WARNING: {city} has only {len(merged)} POIs (need {MIN_PER_CITY})")
        POIS_DIR.joinpath(f"{city}.json").write_text(
            json.dumps([to_json_item(row) for row in merged], ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        for row in merged:
            all_sql_rows.append((city, row))

    if args.dry_run:
        print("dry-run: SQL not written")
        return 0

    write_sql(all_sql_rows)
    after = account_info(key)
    print(f"wrote {SQL_PATH} ({len(all_sql_rows)} rows)")
    print(
        "serpapi credits after: "
        f"{after.get('this_month_usage')} used / "
        f"{after.get('plan_searches_left')} left"
    )
    short = [
        city for city, _ in CITIES.items()
        if sum(1 for c, _ in all_sql_rows if c == city) < MIN_PER_CITY
    ]
    if short:
        raise SystemExit(f"below {MIN_PER_CITY} for: {', '.join(short)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
