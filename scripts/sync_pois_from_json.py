"""Upsert data/pois/*.json into hackathontrip.pois.

Does not DROP/TRUNCATE. Refuses any database other than hackathontrip.
Run from 04app/ after backend/.venv is available:

    python scripts/sync_pois_from_json.py
    python scripts/sync_pois_from_json.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.services.db import ALLOWED_DATABASE, can_connect, get_connection  # noqa: E402

POIS_DIR = REPO_ROOT / "data" / "pois"

SQL = """
INSERT INTO pois (
    slug, name, name_local, city, area, lat, lng,
    requires_ticket, ticket_price, ticket_currency, opening_hours,
    rating, suggested_duration_min, category, source
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, 'seed'
)
ON DUPLICATE KEY UPDATE
    name=VALUES(name),
    name_local=VALUES(name_local),
    area=VALUES(area),
    lat=VALUES(lat),
    lng=VALUES(lng),
    requires_ticket=VALUES(requires_ticket),
    ticket_price=VALUES(ticket_price),
    ticket_currency=VALUES(ticket_currency),
    opening_hours=VALUES(opening_hours),
    rating=VALUES(rating),
    suggested_duration_min=VALUES(suggested_duration_min),
    category=VALUES(category)
"""


def _row(city: str, item: dict) -> tuple:
    slug = item.get("id") or ""
    name_en = item.get("name_en") or item.get("name") or slug
    name_local = item.get("name_local") or (
        item.get("name") if item.get("name_en") and item.get("name") != item.get("name_en") else None
    )
    requires = item.get("requires_ticket")
    if requires in (None, ""):
        requires = "unknown"
    return (
        slug,
        name_en,
        name_local,
        city,
        item.get("area"),
        float(item["lat"]),
        float(item["lng"]),
        str(requires),
        item.get("ticket_price"),
        item.get("ticket_currency"),
        item.get("opening_hours"),
        item.get("rating") or 0,
        item.get("suggested_duration_min") or 90,
        item.get("category") or "attraction",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if settings.mysql_database != ALLOWED_DATABASE:
        print(f"refusing database {settings.mysql_database!r}", file=sys.stderr)
        return 2
    if not can_connect(settings):
        print("MySQL unavailable", file=sys.stderr)
        return 1

    files = sorted(POIS_DIR.glob("*.json"))
    rows: list[tuple] = []
    for path in files:
        city = path.stem
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload:
            if "lat" not in item or "lng" not in item or not item.get("id"):
                continue
            rows.append(_row(city, item))

    print(f"rows={len(rows)} cities={len(files)}")
    if args.dry_run:
        return 0

    conn = get_connection(settings)
    try:
        with conn.cursor() as cur:
            cur.executemany(SQL, rows)
        conn.commit()
    finally:
        conn.close()
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
