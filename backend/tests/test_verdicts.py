"""Verdict-flip tests (T-117): the engine's answer must CHANGE with its
inputs — proving the plans are computed, not decorated. All offline.
"""

from app.agents.tools import draft_day_plan

SIX = ["kinkaku-ji", "kiyomizu-dera", "gion", "sannenzaka", "fushimi-inari", "nishiki-market"]


def test_arrival_time_flips_arrival_day_content():
    base = dict(city="kyoto", days=2, poi_ids=SIX, first_day_density="few", last_day_density="few")
    late = draft_day_plan(arrival_hub_id="kansai-airport", arrival_time="15:30", **base)
    plain = draft_day_plan(**base)
    assert "error" not in late and "error" not in plain
    day1_late = {s["id"] for s in late["days"][0]["spots"]}
    day1_plain = {s["id"] for s in plain["days"][0]["spots"]}
    assert day1_late != day1_plain, "a 15:30 landing must change the arrival day"
    # the flipped arrival day only holds spots that cannot close on you:
    closes = {"kinkaku-ji", "kiyomizu-dera", "nishiki-market"}  # 17:00/18:00 closers
    assert not day1_late & closes or day1_late == day1_plain


def test_day_count_flips_pace_warning():
    over = draft_day_plan("kyoto", 2, SIX)
    ok = draft_day_plan("kyoto", 5, SIX)
    assert any("comfortable pace" in w for w in over["warnings"])
    assert not any("comfortable pace" in w for w in ok["warnings"])


def test_arrival_flips_warning_set():
    with_late = draft_day_plan(
        "kyoto", 2, ["kinkaku-ji", "kiyomizu-dera", "gion"],
        arrival_hub_id="kansai-airport", arrival_time="15:30",
        first_day_density="few", last_day_density="none",
    )
    without = draft_day_plan(
        "kyoto", 2, ["kinkaku-ji", "kiyomizu-dera", "gion"],
        first_day_density="few", last_day_density="none",
    )
    assert "error" not in with_late and "error" not in without
    assert {s["id"] for s in with_late["days"][0]["spots"]} != \
           {s["id"] for s in without["days"][0]["spots"]}, \
           "the landing time must move spots (engine rescues the closers)"
