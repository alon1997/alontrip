"""PoC: Strands Agents + DeepSeek + AlonTrip-engine tools.

Proves the loop the hackathon REQUIRES (Strands Agents SDK) runs with our
existing DeepSeek key over its OpenAI-compatible endpoint, calling tools
that wrap AlonTrip's data and calibrated formulas. Zero AlonTrip code is
copied yet — data files and formulas only; the real build wraps them.

Run: .venv/bin/python poc_agent.py
"""

import json
from pathlib import Path

from strands import Agent, tool
from strands.models.openai import OpenAIModel

ALONTRIP_DATA = Path(__file__).resolve().parents[2] / "01.devnetwork" / "04app" / "data"


def _deepseek_key() -> str:
    env = (ALONTRIP_DATA.parent / ".env").read_text()
    for line in env.splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DEEPSEEK_API_KEY not found in 04app/backend/.env")


@tool
def search_pois(city: str) -> list[dict]:
    """List tourist spots available in a city (Tokyo/Osaka/Kyoto/Seoul/...).

    Each item has: id, name_en, lat, lng, visit_minutes (suggested stay),
    opening_hours (string; may say 'Open 24 hours' or 'Open Closes 6 PM').
    """
    raw = json.loads((ALONTRIP_DATA / "pois" / f"{city}.json").read_text())
    return [
        {
            "id": p["id"], "name_en": p.get("name_en") or p.get("name"),
            "lat": p["lat"], "lng": p["lng"],
            "visit_minutes": p.get("suggested_duration_min", 90),
            "opening_hours": p.get("opening_hours", ""),
        }
        for p in raw
    ]


@tool
def transit_minutes(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> int:
    """Estimated public-transit minutes between two coordinates in an East
    Asian city (formula calibrated on 163 real Google Maps queries)."""
    from math import asin, cos, radians, sin, sqrt

    def km(lat1, lng1, lat2, lng2):
        p = radians((lat1 + lat2) / 2)
        dx = radians(lng2 - lng1) * cos(p) * 111.32
        dy = radians(lat2 - lat1) * 110.57
        return sqrt(dx * dx + dy * dy)

    return min(int(15 + 3.5 * km(a_lat, a_lng, b_lat, b_lng)), 90)


model = OpenAIModel(
    client_args={
        "api_key": _deepseek_key(),
        "base_url": "https://api.deepseek.com/v1",
    },
    model_id="deepseek-chat",
    params={"temperature": 0.2, "max_tokens": 2000},
)

agent = Agent(model=model, tools=[search_pois, transit_minutes])

if __name__ == "__main__":
    result = agent(
        "Plan ONE day in Kyoto for a couple who loves temples and old streets: "
        "pick 3 spots that fit together geographically, order them sensibly, "
        "and make sure each visit can finish before its closing time. "
        "Use the tools to look up real spots and real transit times — do not "
        "invent places. Finish with: the ordered list, transit minutes between "
        "consecutive stops, and one sentence on why the order works."
    )
    print("\n=== FINAL ANSWER ===")
    print(result)
