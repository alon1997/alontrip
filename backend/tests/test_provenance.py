"""Provenance enrichment tests (T-113): server-computed trust badges.

The badges must be derived from the catalog/transit provider, never from
model claims — these tests pin that.
"""

from app.agents.provenance import enrich
from app.agents.tools import draft_day_plan


def _plan(city="kyoto", spots=("kiyomizu-dera", "gion", "sannenzaka")):
    out = draft_day_plan(city, 2, list(spots))
    assert "error" not in out
    return out


def test_all_spots_verified_and_zero_live_calls():
    draft = _plan()
    plan = {"days": [{"day": d["day"], "city": "kyoto",
                      "spots": [{"id": s["id"], "name_en": s["name_en"]} for s in d["spots"]],
                      "hotel": d["hotel"]} for d in draft["days"]],
            "total_usd": 0}
    out = enrich("kyoto", plan)
    p = out["provenance"]
    assert p["spots_total"] == p["spots_catalog_verified"] == 3
    assert p["spots_invented"] == 0 and p["invented_ids"] == []
    assert p["live_api_calls"] == 0


def test_invented_spot_is_flagged():
    draft = _plan()
    plan = {"days": [{"day": 1, "city": "kyoto",
                      "spots": [{"id": s["id"], "name_en": s["name_en"]} for s in draft["days"][0]["spots"]]
                                + [{"id": "made-up-castle", "name_en": "Made Up Castle"}],
                      "hotel": "x"}],
            "total_usd": 0}
    p = enrich("kyoto", plan)["provenance"]
    assert p["spots_invented"] == 1 and p["invented_ids"] == ["made-up-castle"]


def test_duplicated_spot_is_flagged():
    # prod 2026-09-09: the model echoed gion twice in structured output —
    # the badge must catch it (caught live during T-113 verification).
    draft = _plan()
    spots = [{"id": s["id"], "name_en": s["name_en"]} for s in draft["days"][0]["spots"]]
    plan = {"days": [{"day": 1, "city": "kyoto", "spots": spots + [spots[0]], "hotel": "x"}],
            "total_usd": 0}
    p = enrich("kyoto", plan)["provenance"]
    assert p["duplicate_spot_ids"] == [spots[0]["id"]]
    assert p["spots_total"] == len(spots) + 1


def test_day_costs_sum_transit_and_tickets():
    out = enrich("kyoto", {
        "days": [{"day": 1, "spots": [{"id": "kinkaku-ji"}, {"id": "kiyomizu-dera"}]}],
    })
    (c,) = out["day_costs"]
    assert c["day"] == 1
    assert c["transit_usd"] >= 0 and c["tickets_usd"] > 0  # both spots have tickets
    assert c["total_usd"] == round(c["transit_usd"] + c["tickets_usd"], 2)
