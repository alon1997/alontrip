"""T-106 deterministic side: the closing-miss trigger really fires.

The interrupt itself (pause→answer→resume) is agent-driven and covered by
demo_interrupt.py; this test pins the offline half — the engine warning
that *should* make the agent ask — so a silent engine regression can never
strand the demo.
"""

from app.agents.tools import draft_day_plan


def test_late_arrival_early_closing_is_auto_rescued():
    # Kansai 15:30 landing -> post-landing start ~18:50; Kinkaku-ji closes
    # 17:00, so day 1 cannot hold it. The engine's terminal check auto-rescues
    # it to the full day — that's the designed behavior — and the honest pace
    # warning remains as the agent's ask_traveller trigger.
    out = draft_day_plan(
        "kyoto", 2,
        ["kinkaku-ji", "kiyomizu-dera", "gion"],
        arrival_hub_id="kansai-airport",
        arrival_time="15:30",
        first_day_density="few",
        last_day_density="none",
    )
    assert "error" not in out
    day_ids = [ [s["id"] for s in d["spots"]] for d in out["days"] ]
    assert day_ids[0] == ["gion"], "arrival evening must only hold a nearby spot"
    assert set(day_ids[1]) == {"kinkaku-ji", "kiyomizu-dera"}, "rescued spots land on the full day"
    assert out["warnings"], "pace warning should remain for the agent to raise"


def test_move_to_next_day_resolves_it():
    # The auto-responder's answer ("move it to Day 2") must actually clear
    # the warning when re-drafted with kinkaku-ji on the full day.
    day2 = draft_day_plan(
        "kyoto", 2,
        ["kinkaku-ji", "gion"],
        arrival_hub_id="kansai-airport",
        arrival_time="15:30",
        first_day_density="none",
        last_day_density="few",
    )
    assert "error" not in day2
    day_ids = {d["day"]: [s["id"] for s in d["spots"]] for d in day2["days"]}
    assert "kinkaku-ji" in day_ids.get(2, []), "kinkaku-ji should sit on the full day"
    joined = " ".join(day2["warnings"]).lower()
    assert "kinkaku-ji" not in joined
