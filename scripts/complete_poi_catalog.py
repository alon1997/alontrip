"""Fill catalog tickets/hours and convert every ticket to USD.

Adult walk-up prices, mid-2026 backpacker round numbers, same FX as
``app.services.currency`` (JPY/150, CNY/7.2, KRW/1350, HKD/7.8). Streets,
markets, parks, shrines that do not sell a ticket are 0 / requires=no.
Does not invent live FX. Does not DROP anything.

    python scripts/complete_poi_catalog.py
    python scripts/complete_poi_catalog.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.currency import DISPLAY_CURRENCY, to_usd  # noqa: E402

POIS_DIR = REPO_ROOT / "data" / "pois"

# Missing-price slugs only. Values already in USD.
PRICE_USD: dict[str, float] = {
    # Beijing (CNY / 7.2)
    "beijing-ancient-observatory": 2.78,
    "beijing-temple-of-confucius": 4.17,
    "beijing-tours": 0.0,
    "national-centre-for-the-performing-arts": 0.0,  # plaza; shows sold separately
    "neitan": 2.78,
    "wulongting": 0.0,
    "national-stadium": 6.94,
    "underground-city": 2.78,
    "grand-view-garden": 5.56,
    "nine-dragon-screen": 0.0,
    "beijing-world-park": 13.89,
    "fayuan-temple": 0.0,
    "zhengyangmen": 2.78,
    "long-corridor": 0.0,
    "beijing-ming-city-wall-ruins-park": 2.08,
    "temple-of-the-sun": 0.0,
    # Busan
    "haedong-yonggungsa": 0.0,
    "yongdusan-park": 0.0,
    "oryukdo-skywalk": 0.0,
    "songdo-bay-station-songdo-cable-car": 11.11,
    "busan-citizens-park": 0.0,
    "busan-x-the-sky": 20.0,
    "beomeosa-temple": 0.0,
    "choryang-ibagu-gil-alley": 0.0,
    "huinnyeoul-culture-village": 0.0,
    "songdo-sky-park": 0.0,
    "democracy-park": 0.0,
    "busan-modern-and-contemporary-history-museum": 0.0,
    "gukje-market": 0.0,
    "national-maritime-museum-of-korea": 2.22,
    "samnak-ecological-park": 0.0,
    "cheongsapo": 0.0,
    "peace-park": 0.0,
    "geumgang-park": 0.0,
    "f1963": 0.0,
    "apec-naru-park": 0.0,
    "busan-chinatown": 0.0,
    "oryukdo-islets": 0.0,
    "arte-museum-busan": 13.33,
    "cheongsapo-daritdol-skywalk": 0.0,
    "skyline-luge-busan": 16.3,
    "busan-science-center": 3.7,
    # Hong Kong
    "lovers-rock": 0.0,
    "unesco-global-geopark": 0.0,
    "ocean-terminal-deck": 0.0,
    "the-victoria-peak": 0.0,
    "the-garden-of-stars": 0.0,
    "victoria-peak-pavillion": 0.0,
    "tai-kwun": 0.0,
    "yaumatei-tin-hau-temple": 0.0,
    "ancient-kiln-park-and-hong-kong-international-airport-historical-garden": 0.0,
    "sha-tin-che-kung-temple": 0.0,
    "hong-kong-fisherman-s-wharf-sightseeing-boat-tour-aberdeen-1773-1773": 19.23,
    "wan-chai-park": 0.0,
    "hk-city-sightseeing": 0.0,
    "adventureland": 79.0,  # HK Disneyland 1-day ~HK$619
    # Jeju (KRW / 1350) — id "poi" / "poi-2" filled in per-city below
    "manjanggul-lava-tube": 1.48,
    "hallasan-national-park": 0.0,
    "seopjikoji": 0.0,
    "hyeopjae-beach": 0.0,
    "hallim-park": 11.11,
    "yongduam-rock": 0.0,
    "cheonjeyeon-waterfalls": 1.48,
    "jejumok-gwana": 1.11,
    "jeju-folk-village": 8.15,
    "sangumburi-crater": 4.44,
    "mysterious-road": 0.0,
    "halla-arboretum": 0.0,
    "seogwipo-jeongbang-waterfall": 1.48,
    "gimnyeong-maze-park": 8.15,
    "jeju-glass-castle": 8.15,
    "spirited-garden": 8.89,
    "jeju-rail-bike": 11.11,
    "jeju-national-museum": 0.0,
    "jeju-haenyeo-museum": 0.81,
    "aqua-planet-jeju": 31.78,
    "osulloc-tea-museum": 0.0,
    "arte-museum-jeju": 13.33,
    "seongeup-folk-village": 0.0,
    "jeju-samyang-dong-prehistoric-site": 0.0,
    "oedolgae": 0.0,
    "gwangchigi-beach": 0.0,
    "jeju-teseum": 10.37,
    "dodubong-peak": 0.0,
    "jeju-aerospace-museum": 4.44,
    "jeju-4-3-peace-park": 0.0,
    "sinchang-windmill-coastal-road": 0.0,
    "cherry-blossom-tunnel": 0.0,
    # Kyoto
    "arashiyama-2": 0.0,
    "kyoto-sento-imperial-palace": 0.0,
    "kiyamachi-dori": 0.0,
    "daikaku-ji-temple": 3.33,
    "the-flower-corridor": 0.0,
    "uzumasa-kyoto-village": 16.0,
    "kyoto-samurai-experience-waraku": 40.0,
    "kyoto-ry-zen-gokoku-jinja-shrine": 0.0,
    # Osaka
    "nishinomaru-garden": 1.33,
    "the-national-museum-of-art-osaka": 2.87,
    "horie-park": 0.0,
    "dojima-park": 0.0,
    "tengachaya-park": 0.0,
    "osaka-museum-of-natural-history": 2.0,
    "kuchinawazaka": 0.0,
    "peace-osaka-international-peace-center": 0.0,
    "nishi-umeda-park": 0.0,
    "kozu-park": 0.0,
    # Seoul
    "cheonggyecheon": 0.0,
    "gwangjang-market": 0.0,
    "seoullo-7017": 0.0,
    "national-palace-museum-of-korea": 0.0,
    "bukhansan-national-park": 0.0,
    "seoul-children-s-grand-park": 0.0,
    "seoul-museum-of-history": 0.0,
    "namsan-mountain-park": 0.0,
    "yongsan-family-park": 0.0,
    "central-asia-road": 0.0,
    "seoul-grand-park": 3.7,
    "seoul-grand-park-skylift": 5.93,
    "k-star-road": 0.0,
    "yongsan-haebangchon-village": 0.0,
    "naksan-park": 0.0,
    # Shanghai
    "dongfangmingzhu-pleasure-cruise-boat-wharf": 16.67,
    "shanghai-circus-world": 27.78,
    # Tokyo
    "tokyo-dome-city": 26.0,
}

CITY_PRICE_USD: dict[tuple[str, str], float] = {
    ("beijing", "poi"): 4.17,  # 鼓楼钟楼
    ("jeju", "poi"): 8.15,  # 일출랜드
    ("jeju", "poi-2"): 0.0,  # 중문관광단지 outdoor
}

HOURS: dict[str, str] = {
    "senso-ji": "Open · Closes 5 PM",
    "fushimi-inari": "Open 24 hours",
    "meiji-jingu": "Open · Closes 5 PM",
    "imperial-palace": "Open · Closes 5 PM",
    "tokyo-disney-resort": "Open · Closes 9 PM",
    "tokyo-disneyland": "Open · Closes 9 PM",
    "tokyo-disneysea": "Open · Closes 9 PM",
    "universal-studios-japan": "Open · Closes 9 PM",
    "shanghai-disneyland-park": "Open · Closes 9 PM",
    "happy-valley-beijing": "Open · Closes 9 PM",
    "tokyo-dome-city": "Open · Closes 10 PM",
    "tsukiji-market": "Open · Closes 2 PM",
    "nishiki-market": "Open · Closes 6 PM",
    "gukje-market": "Open · Closes 8 PM",
    "gwangjang-market": "Open · Closes 11 PM",
    "temple-street": "Open · Closes 11 PM",
    "the-peak-tram": "Open · Closes 12 AM",
    "ngong-ping-360": "Open · Closes 6 PM",
    "victoria-peak": "Open 24 hours",
    "tiananmen-square": "Open · Closes 10 PM",
    "badaling": "Open · Closes 5 PM",
    "lotte-world-tower": "Open · Closes 10 PM",
}

DURATION: dict[str, int] = {
    "hallasan-national-park": 360,
    "bukhansan-national-park": 300,
    "shanghai-circus-world": 120,
    "tokyo-dome-city": 240,
    "adventureland": 540,
    "happy-valley-beijing": 420,
}

FIELD_ORDER = [
    "id", "name", "name_en", "name_local", "lat", "lng", "rating",
    "suggested_duration_min", "category", "area", "opening_hours",
    "requires_ticket", "ticket_price", "ticket_currency",
]


def _hours_for(poi: dict) -> str:
    slug = poi["id"]
    if slug in HOURS:
        return HOURS[slug]
    existing = (poi.get("opening_hours") or "").strip()
    if existing:
        return existing
    cat = (poi.get("category") or "").lower()
    name = f"{poi.get('name_en') or ''} {poi.get('name') or ''}".lower()
    if any(k in cat or k in name for k in ("district", "street", "plaza", "square", "village", "waterfront", "beach", "bridge")):
        return "Open 24 hours"
    if any(k in cat or k in name for k in ("market",)):
        return "Open · Closes 8 PM"
    if any(k in cat or k in name for k in ("theme park", "disney", "universal", "joypolis")):
        return "Open · Closes 9 PM"
    if any(k in cat or k in name for k in ("museum", "aquarium", "gallery", "observatory")):
        return "Open · Closes 5 PM"
    if any(k in cat or k in name for k in ("temple", "shrine", "palace", "garden")):
        return "Open · Closes 5 PM"
    if any(k in cat or k in name for k in ("park", "nature", "trail", "mountain", "crater")):
        return "Open · Closes 6 PM"
    return "Open · Closes 6 PM"


def _price_usd(city: str, poi: dict) -> float:
    key = (city, poi["id"])
    if key in CITY_PRICE_USD:
        return CITY_PRICE_USD[key]
    if poi.get("ticket_price") is not None:
        converted = to_usd(float(poi["ticket_price"]), poi.get("ticket_currency") or "USD")
        if converted is None:
            raise ValueError(f"{city}/{poi['id']} ticket_price not convertible")
        return converted
    if poi["id"] in PRICE_USD:
        return PRICE_USD[poi["id"]]
    raise KeyError(f"no USD ticket for {city}/{poi['id']} ({poi.get('name_en')})")


def complete_one(city: str, poi: dict) -> dict:
    price = _price_usd(city, poi)
    requires = "yes" if price > 0 else "no"
    duration = poi.get("suggested_duration_min") or DURATION.get(poi["id"]) or 90
    if poi["id"] in DURATION:
        duration = DURATION[poi["id"]]
    out = dict(poi)
    out["ticket_price"] = price
    out["ticket_currency"] = DISPLAY_CURRENCY
    out["requires_ticket"] = requires
    out["opening_hours"] = _hours_for(poi)
    out["suggested_duration_min"] = int(duration)
    ordered = {k: out[k] for k in FIELD_ORDER if k in out and out[k] is not None and out[k] != ""}
    for k, v in out.items():
        if k not in ordered and v is not None and v != "":
            ordered[k] = v
    return ordered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    missing = 0
    written = 0
    for path in sorted(POIS_DIR.glob("*.json")):
        city = path.stem
        raw = json.loads(path.read_text(encoding="utf-8"))
        completed = []
        for poi in raw:
            try:
                completed.append(complete_one(city, poi))
            except KeyError as exc:
                print(exc, file=sys.stderr)
                missing += 1
                completed.append(poi)
        currencies = {p.get("ticket_currency") for p in completed}
        none_price = [p["id"] for p in completed if p.get("ticket_price") is None]
        none_hours = [p["id"] for p in completed if not p.get("opening_hours")]
        print(f"{city}: n={len(completed)} currency={currencies} no_price={none_price} no_hours={none_hours}")
        if not args.dry_run and missing == 0:
            path.write_text(json.dumps(completed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written += 1
    if missing:
        print(f"refusing to write: {missing} slugs still have no price", file=sys.stderr)
        return 1
    print("ok" if not args.dry_run else "dry-run ok", f"files={written if not args.dry_run else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
