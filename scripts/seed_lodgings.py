"""Seed catalog hostels/capsules into hackathontrip.lodgings.

Same pattern as POIs (D-015): pre-fill a city catalog, keep custom hotel
search for later. This script only writes listed=1 budget stays.

    python scripts/seed_lodgings.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from seed_pois import (
    CITIES,
    ENV_FILE,
    account_info,
    area_from_address,
    fetch_page,
    haversine_m,
    read_api_key,
    sql_num,
    sql_str,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.poi import _slugify, _unique_id  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "data" / "lodgings"
CACHE_DIR = REPO_ROOT / "data" / "lodging_serpapi_cache"
SQL_PATH = REPO_ROOT / "db" / "seed_lodgings.sql"
MIN_PER_CITY = 8
MATCH_METERS = 120

KEEP_HINTS = (
    "hostel",
    "capsule",
    "backpacker",
    "youth hostel",
    "guest house",
    "guesthouse",
)
SKIP_HINTS = ("love hotel", "love motel", "adult")


def cache_path(city: str, query: str, start: int) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:80]
    return CACHE_DIR / f"{city}__{slug}__start{start}.json"


def kind_of(raw_type: str, name: str) -> str | None:
    blob = f"{raw_type} {name}".casefold()
    if any(token in blob for token in SKIP_HINTS):
        return None
    if "capsule" in blob:
        return "capsule"
    if "hostel" in blob or "backpacker" in blob:
        return "hostel"
    if "guest house" in blob or "guesthouse" in blob:
        return "guesthouse"
    return None


def parse_nightly_price(raw: object, default_ccy: str) -> tuple[float | None, str | None]:
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text or re.fullmatch(r"\$+", text):
        return None, None
    match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    if not match:
        return None, None
    amount = float(match.group(1))
    if amount <= 0 or amount > 200000:
        return None, None
    lower = text.casefold()
    if "yen" in lower or "jpy" in lower or "¥" in text or "￥" in text:
        return amount, "JPY"
    if "won" in lower or "krw" in lower:
        return amount, "KRW"
    if "yuan" in lower or "cny" in lower or "\u5143" in text:
        return amount, "CNY"
    if "hkd" in lower or "hk$" in lower:
        return amount, "HKD"
    if "$" in text:
        return amount, "USD"
    return None, None


def queries_for(city: str, label: str) -> list[str]:
    queries = [f"backpacker hostel {label}"]
    if city in {"tokyo", "kyoto", "osaka"}:
        queries.append(f"capsule hotel {label}")
    else:
        queries.append(f"youth hostel {label}")
    return queries


def parse_results(payload: dict, city: str, default_ccy: str) -> list[dict]:
    rows = []
    for raw in payload.get("local_results") or []:
        gps = raw.get("gps_coordinates") or {}
        lat, lng = gps.get("latitude"), gps.get("longitude")
        title = str(raw.get("title") or "").strip()
        raw_type = str(raw.get("type") or "")
        if lat is None or lng is None or not title:
            continue
        kind = kind_of(raw_type, title)
        if not kind:
            continue
        price, ccy = parse_nightly_price(raw.get("price"), default_ccy)
        rating = raw.get("rating")
        rows.append(
            {
                "name": title,
                "lat": float(lat),
                "lng": float(lng),
                "rating": float(rating) if rating is not None else None,
                "area": area_from_address(str(raw.get("address") or ""), city),
                "kind": kind,
                "listed": 1,
                "price_per_night": price,
                "price_currency": ccy if price is not None else None,
                "place_id": str(raw.get("place_id") or raw.get("data_id") or "").strip() or None,
                "source": "seed",
            }
        )
    return rows


def fetch_city(key: str, city: str, meta: dict, dry_run: bool) -> list[dict]:
    collected: list[dict] = []
    seen: set[str] = set()
    for query in queries_for(city, meta["label"]):
        if len(collected) >= MIN_PER_CITY:
            break
        for start in (0, 20):
            if len(collected) >= MIN_PER_CITY:
                break
            cache_file = cache_path(city, query, start)
            if dry_run and not cache_file.exists():
                print(f"  would fetch {city} q={query!r} start={start}")
                continue
            payload = fetch_page(key, query, meta["ll"], start, cache_file)
            page = parse_results(payload, city, meta["currency"])
            print(f"  {city} {query!r} start={start}: {len(page)} hostels (cache={cache_file.name})")
            for row in page:
                key_name = row["name"].casefold()
                if key_name in seen:
                    continue
                dup = False
                for existing in collected:
                    if haversine_m(existing["lat"], existing["lng"], row["lat"], row["lng"]) <= MATCH_METERS:
                        dup = True
                        break
                if dup:
                    continue
                seen.add(key_name)
                collected.append(row)
            if len(page) < 5:
                break
    return collected


def to_json_item(row: dict) -> dict:
    item = {
        "id": row["slug"],
        "name": row["name"],
        "lat": round(float(row["lat"]), 7),
        "lng": round(float(row["lng"]), 7),
        "kind": row["kind"],
        "listed": 1,
        "rating": float(row["rating"]) if row.get("rating") is not None else 0.0,
    }
    if row.get("area"):
        item["area"] = row["area"]
    if row.get("price_per_night") is not None:
        item["price_per_night"] = row["price_per_night"]
        item["price_currency"] = row.get("price_currency")
    return item


def write_sql(all_rows: list[tuple[str, dict]]) -> None:
    lines = [
        "-- Catalog lodgings for hackathontrip.lodgings (D-015).",
        "-- listed=1 budget stays only. Nightly price left NULL unless the API gave a number.",
        "SET NAMES utf8mb4;",
        "",
    ]
    for city, row in all_rows:
        lines.append(
            "INSERT INTO lodgings ("
            "slug, name, name_local, city, area, lat, lng, kind, listed, "
            "price_per_night, price_currency, rating, source, place_id"
            ") VALUES ("
            f"{sql_str(row['slug'])}, "
            f"{sql_str(row['name'])}, "
            "NULL, "
            f"{sql_str(city)}, "
            f"{sql_str(row.get('area'))}, "
            f"{sql_num(row['lat'], 7)}, "
            f"{sql_num(row['lng'], 7)}, "
            f"{sql_str(row['kind'])}, 1, "
            f"{sql_num(row.get('price_per_night'), 2)}, "
            f"{sql_str(row.get('price_currency'))}, "
            f"{sql_num(row.get('rating'), 2) if row.get('rating') is not None else 'NULL'}, "
            "'seed', "
            f"{sql_str(row.get('place_id'))}"
            ");"
        )
    SQL_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = read_api_key()
    before = account_info(key)
    print(
        "serpapi credits: "
        f"{before.get('this_month_usage')} used / "
        f"{before.get('plan_searches_left')} left this plan"
    )

    all_rows: list[tuple[str, dict]] = []
    seen_slugs_global: dict[str, set[str]] = {city: set() for city in CITIES}
    for city, meta in CITIES.items():
        fetched = fetch_city(key, city, meta, args.dry_run)
        for row in fetched:
            row["slug"] = _unique_id(_slugify(row["name"]), seen_slugs_global[city])
            all_rows.append((city, row))
        print(f"{city}: {len(fetched)}")
        if not args.dry_run:
            (OUT_DIR / f"{city}.json").write_text(
                json.dumps([to_json_item(row) for _, row in all_rows if _ == city], ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )

    if args.dry_run:
        return 0

    write_sql(all_rows)
    after = account_info(key)
    print(f"wrote {SQL_PATH} ({len(all_rows)} rows)")
    print(
        "serpapi credits after: "
        f"{after.get('this_month_usage')} used / "
        f"{after.get('plan_searches_left')} left"
    )
    short = [
        city
        for city in CITIES
        if sum(1 for c, _ in all_rows if c == city) < MIN_PER_CITY
    ]
    if short:
        print("WARNING below 8:", ", ".join(short))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
