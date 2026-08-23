"""Fill ticket fields from OpenStreetMap fee/charge tags.

SerpApi Google Maps has no admission-price field (verified on the 8-city cache).
Wikidata SPARQL is blocked from this network. OSM does tag many museums,
temples, and parks with fee=yes/no and sometimes charge=*.

Match OSM objects to our catalog by coordinates (and name when several are
nearby). Write only what the tags support. Unmatched rows stay unknown.

    python scripts/seed_tickets.py
    python scripts/seed_tickets.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import unicodedata
from pathlib import Path

import httpx

from seed_pois import CITIES, POIS_DIR, haversine_m

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "data" / "osm_fee_cache"
SQL_PATH = REPO_ROOT / "db" / "seed_tickets.sql"

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
SKIP_POI_CATEGORIES = {"district"}
EXTRA_OSM_HINTS = {
    "museum",
    "lighting",
    "aquarium",
    "zoo",
    "view",
    "observation",
    "gallery",
    "cafe",
    "restaurant",
    "shop",
}
NAME_M = 700
PIN_M = 80
BBOX_PAD = 0.12

CURRENCY_HINT = {
    "yen": "JPY",
    "jpy": "JPY",
    "円": "JPY",
    "won": "KRW",
    "krw": "KRW",
    "yuan": "CNY",
    "cny": "CNY",
    "rmb": "CNY",
    "元": "CNY",
    "hkd": "HKD",
}


def load_pois(city: str) -> list[dict]:
    return json.loads((POIS_DIR / f"{city}.json").read_text(encoding="utf-8"))


def bbox_of(pois: list[dict]) -> tuple[float, float, float, float]:
    lats = [float(p["lat"]) for p in pois]
    lngs = [float(p["lng"]) for p in pois]
    return (
        min(lats) - BBOX_PAD,
        min(lngs) - BBOX_PAD,
        max(lats) + BBOX_PAD,
        max(lngs) + BBOX_PAD,
    )


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return f"""[out:json][timeout:90];
(
  nwr["tourism"]["fee"]({s},{w},{n},{e});
  nwr["historic"]["fee"]({s},{w},{n},{e});
  nwr["leisure"~"park|garden"]["fee"]({s},{w},{n},{e});
  nwr["amenity"="place_of_worship"]["fee"]({s},{w},{n},{e});
  nwr["tourism"]["charge"]({s},{w},{n},{e});
  nwr["historic"]["charge"]({s},{w},{n},{e});
);
out tags center;"""


def overpass_around_query(pois: list[dict]) -> str:
    parts: list[str] = []
    for poi in pois:
        lat, lng = float(poi["lat"]), float(poi["lng"])
        parts.append(f'  nwr["tourism"]["fee"](around:700,{lat},{lng});')
        parts.append(f'  nwr["historic"]["fee"](around:700,{lat},{lng});')
        parts.append(f'  nwr["leisure"~"park|garden"]["fee"](around:700,{lat},{lng});')
        parts.append(f'  nwr["amenity"="place_of_worship"]["fee"](around:700,{lat},{lng});')
        parts.append(f'  nwr["tourism"]["charge"](around:700,{lat},{lng});')
    body = "\n".join(parts)
    return f"""[out:json][timeout:90];
(
{body}
);
out tags center;"""


def post_overpass(query: str, cache: Path) -> list[dict]:
    last_error = None
    for url in OVERPASS_URLS:
        try:
            resp = httpx.post(
                url,
                content=query.encode(),
                headers={"User-Agent": "hackathontrip-seed/1.0"},
                timeout=100.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            time.sleep(6)
            return payload.get("elements") or []
        except Exception as exc:
            last_error = exc
            time.sleep(3)
    raise RuntimeError(str(last_error))


def load_cached(city: str) -> list[dict] | None:
    for name in (f"{city}.json", f"{city}_around.json"):
        cache = CACHE_DIR / name
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8")).get("elements") or []
    return None


def fetch_osm(city: str, bbox: tuple[float, float, float, float], pois: list[dict]) -> list[dict]:
    cached = load_cached(city)
    if cached is not None:
        return cached
    try:
        return post_overpass(overpass_query(bbox), CACHE_DIR / f"{city}.json")
    except RuntimeError:
        return post_overpass(overpass_around_query(pois), CACHE_DIR / f"{city}_around.json")


def osm_point(el: dict) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def names_of(tags: dict) -> list[str]:
    out = []
    for key in ("name:en", "name", "name:zh", "name:ja", "name:ko", "alt_name"):
        val = tags.get(key)
        if val:
            out.append(val)
    return out


def norm_name(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", s)
    return " ".join(s.split())


def names_similar(a: str, b: str) -> bool:
    na, nb = norm_name(a), norm_name(b)
    if len(na) < 3 or len(nb) < 3:
        return False
    if na == nb:
        return True
    sa, sb = set(na.split()), set(nb.split())
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    extra = longer - shorter
    if extra & EXTRA_OSM_HINTS:
        return False
    if shorter <= longer:
        return len(shorter) >= 2
    overlap = sa & sb
    return len(overlap) >= 2 and len(overlap) / len(sa | sb) >= 0.6


def poi_matches_osm(poi: dict, osm_names: list[str]) -> bool:
    poi_names = [poi.get("name_en") or "", poi.get("name") or ""]
    for pn in poi_names:
        for on in osm_names:
            if names_similar(pn, on):
                return True
    return False


def parse_amount(text: str, default_ccy: str) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    raw = text.replace(",", " ")
    lower = raw.casefold()
    ccy = default_ccy
    for hint, code in CURRENCY_HINT.items():
        if hint in lower:
            ccy = code
            break
    match = re.search(r"(?:¥|￥|₩)?\s*(\d+(?:\.\d+)?)", raw)
    if not match:
        return None, None
    amount = float(match.group(1))
    if amount <= 0 or amount > 200000:
        return None, None
    return amount, ccy


def interpret_tags(tags: dict, default_ccy: str) -> dict | None:
    fee = str(tags.get("fee") or "").strip().casefold()
    charge = str(tags.get("charge") or tags.get("fee:amount") or tags.get("charge:adult") or "").strip()
    if fee in {"no", "donation"}:
        return {
            "requires_ticket": "no",
            "ticket_price": 0,
            "ticket_currency": default_ccy,
        }
    amount, ccy = parse_amount(fee, default_ccy)
    if amount is not None and fee not in {"yes", "unknown"}:
        return {
            "requires_ticket": "yes",
            "ticket_price": amount,
            "ticket_currency": ccy,
        }
    if fee in {"yes"} or charge:
        parsed, pccy = parse_amount(charge, default_ccy)
        if parsed is not None:
            return {
                "requires_ticket": "yes",
                "ticket_price": parsed,
                "ticket_currency": pccy,
            }
        if fee in {"yes"} or charge:
            return {
                "requires_ticket": "yes",
                "ticket_price": None,
                "ticket_currency": None,
            }
    return None


def pick_match(poi: dict, elements: list[dict], default_ccy: str) -> dict | None:
    if str(poi.get("category") or "").lower() in SKIP_POI_CATEGORIES:
        return None
    nearby: list[tuple[float, dict, dict]] = []
    for el in elements:
        point = osm_point(el)
        tags = el.get("tags") or {}
        if not point or not tags:
            continue
        dist = haversine_m(float(poi["lat"]), float(poi["lng"]), point[0], point[1])
        if dist > NAME_M:
            continue
        parsed = interpret_tags(tags, default_ccy)
        if not parsed:
            continue
        nearby.append((dist, parsed, tags))
    if not nearby:
        return None
    nearby.sort(key=lambda row: row[0])
    named = [
        row for row in nearby
        if poi_matches_osm(poi, names_of(row[2]))
    ]
    if named:
        return named[0][1]
    close = [row for row in nearby if row[0] <= PIN_M]
    if len(close) == 1:
        return close[0][1]
    return None


def sql_num(value: object, decimals: int | None = None) -> str:
    if value is None:
        return "NULL"
    if decimals is None:
        return str(int(value))
    return f"{float(value):.{decimals}f}"


def sql_str(value: object) -> str:
    if value is None or value == "":
        return "NULL"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    updates: list[tuple[str, str, dict]] = []
    stats = []

    for city, meta in CITIES.items():
        pois = load_pois(city)
        cached = load_cached(city)
        if cached is not None:
            elements = cached
        else:
            if args.dry_run:
                print(f"{city}: would fetch OSM")
                continue
            elements = fetch_osm(city, bbox_of(pois), pois)

        hit = 0
        priced = 0
        for poi in pois:
            poi.pop("requires_ticket", None)
            poi.pop("ticket_price", None)
            poi.pop("ticket_currency", None)
            parsed = pick_match(poi, elements, meta["currency"])
            if not parsed:
                continue
            hit += 1
            if parsed["ticket_price"] is not None:
                priced += 1
            poi["requires_ticket"] = parsed["requires_ticket"]
            if parsed["ticket_price"] is not None:
                poi["ticket_price"] = parsed["ticket_price"]
                poi["ticket_currency"] = parsed["ticket_currency"]
            updates.append((city, poi["id"], parsed))
        stats.append((city, len(pois), hit, priced, len(elements)))
        print(f"{city}: osm {len(elements)} tagged, matched {hit}/{len(pois)} (priced {priced})")
        if not args.dry_run:
            (POIS_DIR / f"{city}.json").write_text(
                json.dumps(pois, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    print("total matched", len(updates), "/", sum(s[1] for s in stats))
    if args.dry_run:
        return 0

    lines = [
        "-- Ticket overlay from OSM fee/charge (D-014).",
        "-- Only UPDATE rows we matched. Unmatched stay unknown.",
        "SET NAMES utf8mb4;",
        "",
    ]
    for city, slug, parsed in updates:
        lines.append(
            "UPDATE pois SET "
            f"requires_ticket={sql_str(parsed['requires_ticket'])}, "
            f"ticket_price={sql_num(parsed['ticket_price'], 2)}, "
            f"ticket_currency={sql_str(parsed['ticket_currency'])} "
            f"WHERE city={sql_str(city)} AND slug={sql_str(slug)};"
        )
    SQL_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {SQL_PATH} ({len(updates)} updates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
