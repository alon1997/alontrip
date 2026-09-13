"""Offline unit tests for the agent tool layer (T-103).

No network, no API keys: everything runs on the vendored catalog data and
the engine's calibrated estimator. Run from the repo root: ``pytest``.
"""

from app.agents.tools import (
    draft_day_plan,
    list_cities,
    replay_clock,
    search_pois,
    spot_detail,
    transit_route,
    trip_budget,
)


def test_list_cities_has_nine_cities():
    cities = list_cities()
    ids = [c["city"] for c in cities]
    assert "kyoto" in ids and "osaka" in ids and len(ids) >= 9
    assert all(c["spots"] > 0 for c in cities)


def test_search_pois_and_filter():
    all_spots = search_pois("kyoto")
    assert len(all_spots) >= 20
    assert {"id", "name_en", "lat", "lng", "visit_minutes", "opening_hours"} <= set(all_spots[0])
    filtered = search_pois("kyoto", query="kiyomizu")
    assert filtered and filtered[0]["id"] == "kiyomizu-dera"


def test_spot_detail_parses_closing_time():
    d = spot_detail("kyoto", "kiyomizu-dera")
    assert d["closes"] == "18:00"  # "Open Closes 6 PM"
    assert d["visit_minutes"] >= 30


def test_transit_route_returns_minutes_and_source():
    r = transit_route(34.9949, 135.785, 34.9966644, 135.781008, city="kyoto")
    assert 0 < r["minutes"] <= 90
    assert isinstance(r["estimated"], bool)
    assert r["source"] in ("estimate", "serpapi_cache", "walk")


def test_draft_day_plan_groups_and_warns_or_not():
    spots = [s["id"] for s in search_pois("kyoto")][:5]
    out = draft_day_plan("kyoto", 3, spots)
    assert "error" not in out
    placed = [s["id"] for d in out["days"] for s in d["spots"]]
    assert sorted(placed) == sorted(spots)
    assert all(d["hotel"] for d in out["days"])


def test_draft_day_plan_rejects_unknown_id():
    out = draft_day_plan("kyoto", 2, ["no-such-spot"])
    assert out["error"].startswith("unknown poi_ids")


def test_replay_clock_produces_timed_visit_rows():
    day = draft_day_plan("kyoto", 2, ["kiyomizu-dera", "sannenzaka"])["days"][0]
    sched = replay_clock("kyoto", day)
    visits = [e for e in sched["events"] if e["kind"] == "visit"]
    assert {v["title"] for v in visits} == {"Kiyomizu-dera", "Sannenzaka"}
    assert all(v["start"] <= v["end"] for v in visits)


def test_trip_budget_totals_positive_usd():
    days = draft_day_plan("kyoto", 2, ["kiyomizu-dera", "sannenzaka", "gion"])["days"]
    budget = trip_budget("kyoto", days)
    assert budget["total_usd"] >= 0
    assert {"transit_usd", "tickets_usd", "stays_usd", "unknown_prices"} <= set(budget)
