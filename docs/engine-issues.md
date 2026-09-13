# Engine issues found while building TripAgent

Found during batch 2/3 (2026-09-06). The vendored copy is unmodified by
design; these go back upstream to [alontrip](https://github.com/alon1997/alontrip)
with the PM's approval after 9/9.

| # | Where | What | Impact | Workaround here |
|---|-------|------|--------|-----------------|
| 1 | `hours.py parse_hours` | `"Open Closes 17:00"` / `"Closes 08:30"` (24-h clock) parse as unknown → treated as always open. Only AM/PM forms parse after "Closes". | Any catalog entry stored in 24-h style is invisible to window feasibility, clock clipping, and the terminal check. Current 328-entry catalog uses AM/PM, so production unaffected. | `agent/watcher.py` injects overrides as "H:MM AM/PM" |
| 2 | `main.py` warning reconciliation | (upstream observation, not hit here) unvisited warnings rely on visit-title matching; spots with identical names across cities would double-count. Single-city tool calls make this impossible in TripAgent. | — | none needed |

Also fixed on OUR side (not engine bugs):
- `agent/tools.py draft_day_plan` initially dropped `arrival_time` instead of
  converting HH:MM → minutes (caught by `tests/test_decisions.py` on first run).
- `web/index.html` SSE parser initially fed `data: {...}` lines to
  `JSON.parse` without stripping the prefix.
