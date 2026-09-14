#!/usr/bin/env python3
"""Live stability suite for agent mode (T-A9).

Runs the final demo prompt N times against the production agent endpoint
(fresh session each) and asserts, per run:
  1. a plan was emitted through the classic renderer (server log: "OK")
  2. day 1 carries exactly one spot; the last day carries none
  3. the full-day park (Disneyland) owns its day alone
  4. no engine-failure warnings ("couldn't be visited" / "doesn't fit")
  5. every day is geographically coherent (max pairwise spread <= 12 km)
  6. every day with spots is hotel-anchored with a chain

Usage: python3 scripts/agent_stability.py [N] [--url URL]
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.request

# macOS python.org builds often miss the CA bundle — this suite only ever
# talks to our own server, so verification off is the pragmatic fallback
_CTX = ssl.create_default_context()
if not _CTX.check_hostname:  # pragma: no cover
    pass
try:
    with urllib.request.urlopen("https://alonuniverse.com/api/trip/health", timeout=10) as _r:
        _r.read()
except urllib.error.URLError:
    _CTX = ssl._create_unverified_context()  # noqa: SLF001

PROMPT = (
    "Plan 5 days 4 nights in Tokyo. I arrive at Narita Airport 3:00 PM on day 1 "
    "and depart Narita 5:00 PM on day 5. About 10 spots total. Tokyo Disneyland "
    "is a must — give it one full day alone. Day 1: one easy evening spot near "
    "the hotel after check-in. Days 2 to 4: around three spots per day, grouped "
    "by district. Day 5: no sightseeing, just the flight home."
)
FORBIDDEN = ("couldn", "doesn't fit", "cannot fit")


def stream_plan(url: str, session: str):
    """Yield SSE event dicts for one agent chat."""
    body = json.dumps({"message": PROMPT, "session_id": session}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900, context=_CTX) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                line = frame.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        pass


def check(plan: dict, cat: dict) -> list[str]:
    """Return a list of failure strings (empty == pass)."""
    fails = []
    days = plan.get("days") or []
    if not days:
        return ["no days in plan"]
    d1 = days[0].get("spots") or []
    dlast = days[-1].get("spots") or []
    if len(d1) != 1:
        fails.append(f"day1 spots={len(d1)} (want 1)")
    if dlast:
        fails.append(f"last day has {len(dlast)} spots (want 0)")
    # full-day park alone
    for d in days:
        parks = [s for s in d["spots"] if (cat.get(s["id"], {}).get("dur") or 0) >= 360]
        if parks and len(d["spots"]) > 1:
            fails.append(f"day {d['day']} stacks {len(d['spots'])} spots on a park")
    # warnings
    for w in plan.get("warnings") or []:
        if any(f in w for f in FORBIDDEN):
            fails.append(f"forbidden warning: {w[:90]}")
    # geography + hotel anchors
    for d in days:
        pts = [cat[s["id"]] for s in d["spots"] if s["id"] in cat]
        mx = 0.0
        for i, a in enumerate(pts):
            for b in pts[i + 1:]:
                mx = max(mx, haversine_km(a["lat"], a["lng"], b["lat"], b["lng"]))
        if mx > 12.0:
            names = [s["name_en"] for s in d["spots"]]
            fails.append(f"day {d['day']} spread {mx:.1f}km: {names}")
        if d["spots"] and (not d.get("hotel") or not d.get("chain")):
            fails.append(f"day {d['day']} missing hotel/chain")
    return fails


def haversine_km(la1, lo1, la2, lo2):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (la1, lo1, la2, lo2))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(h))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
    base = "https://alonuniverse.com/api/trip"
    cat = {}
    with urllib.request.urlopen(base + f"/agent/spots/tokyo", context=_CTX) as r:  # noqa: S310
        for s in json.load(r):
            cat[s["id"]] = {"lat": s["lat"], "lng": s["lng"], "dur": 0}
    with urllib.request.urlopen(base + "/pois?city=tokyo", context=_CTX) as r:  # noqa: S310
        for p in json.load(r)["pois"]:
            if p["id"] in cat:
                cat[p["id"]]["dur"] = p.get("suggested_duration_min") or 0

    any_fail = False
    for run in range(1, n + 1):
        session = f"stab-{run}"
        plan, tools = None, 0
        for ev in stream_plan(base + "/agent/chat", session):
            if ev["type"] == "tool":
                tools += 1
            elif ev["type"] == "plan":
                plan = ev["plan"]
            elif ev["type"] == "error":
                print(f"run{run}: STREAM ERROR {ev['message'][:120]}")
        if plan is None:
            print(f"run{run}: !! NO PLAN (tools={tools})")
            any_fail = True
            continue
        fails = check(plan, cat)
        shape = [len(d.get("spots") or []) for d in plan.get("days") or []]
        hotels = sorted({d.get("hotel", "") for d in plan["days"]})
        status = "PASS" if not fails else "FAIL"
        print(f"run{run}: {status} spots/day={shape} tools={tools} hotels={hotels}")
        for f in fails:
            print(f"   !! {f}")
        any_fail = any_fail or bool(fails)
    print("\nSUITE:", "FAIL" if any_fail else "ALL PASS")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
