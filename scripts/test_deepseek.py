"""T-007: DeepSeek API connectivity smoke test.

Not a product path. Proves the key in `.env` can reach DeepSeek's
Anthropic-compatible endpoint and get back a usable grouping JSON. Does not
touch `POST /optimize-route` or the production grouping prompt (T-011).

Usage (from 04app/):
    python3 scripts/test_deepseek.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]  # 04app/
DEEPSEEK_MODEL = "deepseek-v4-pro"  # must match grouping.py's DEEPSEEK_MODEL

TOKYO_POI_IDS = [
    "senso-ji",
    "akihabara",
    "shibuya-crossing",
    "shinjuku",
    "harajuku",
    "ginza",
]


def _load_env(path: Path) -> dict[str, str]:
    """Minimal .env parser — avoids pulling in Settings' required-fields
    validation just to read two values for a standalone smoke test."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _build_prompt() -> str:
    ids = ", ".join(TOKYO_POI_IDS)
    return (
        "Group the following Tokyo POI ids into exactly 2 day-by-day groups. "
        "Reply with ONLY a JSON object of the form "
        '{"days": [["id", ...], ["id", ...]]} using every id exactly once, '
        f"no id repeated, no extra text.\n\nids: {ids}"
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in reply") from None
        return json.loads(text[start : end + 1])


def _find_text_block(content: list[dict]) -> str:
    """deepseek-v4-pro is a reasoning model: content[0] is a "thinking" block,
    not the reply. The actual answer is the first block with type "text".
    (This also affects production LLMGrouper._call_llm — flagged for T-011.)
    """
    for block in content:
        if block.get("type") == "text":
            return block.get("text", "")
    raise ValueError("no text block in reply content")


def _call_once(api_key: str, base_url: str) -> tuple[int, dict | None, str]:
    """Returns (http_status, parsed_json_or_None, raw_text_for_debug)."""
    resp = httpx.post(
        f"{base_url.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            # Reasoning model: thinking tokens count against max_tokens too,
            # so this must comfortably exceed the thinking budget or the
            # reply gets cut off before the text block appears.
            "model": DEEPSEEK_MODEL,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": _build_prompt()}],
        },
        timeout=20.0,
    )
    raw_text = resp.text
    if resp.status_code // 100 != 2:
        return resp.status_code, None, raw_text
    try:
        text = _find_text_block(resp.json()["content"])
        parsed = _extract_json(text)
    except Exception:  # noqa: BLE001 — any parse failure is a FAIL, not a crash
        return resp.status_code, None, raw_text
    return resp.status_code, parsed, raw_text


def _validate(parsed: dict) -> tuple[bool, str]:
    days = parsed.get("days")
    if not isinstance(days, list) or len(days) != 2:
        return False, "'days' is not a list of length 2"
    flat = [poi_id for group in days for poi_id in group]
    if len(flat) != len(TOKYO_POI_IDS) or set(flat) != set(TOKYO_POI_IDS):
        return False, f"ids mismatch: got {flat}"
    if len(flat) != len(set(flat)):
        return False, f"duplicate ids: {flat}"
    return True, ""


def _attempt(api_key: str, base_url: str, attempt_no: int) -> bool:
    t0 = time.monotonic()
    status, parsed, raw_text = _call_once(api_key, base_url)
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if status // 100 != 2:
        print(f"FAIL (attempt {attempt_no}): HTTP {status}, elapsed {elapsed_ms}ms")
        print(raw_text[:500])
        return False

    if parsed is None:
        print(f"FAIL (attempt {attempt_no}): could not extract JSON from reply, elapsed {elapsed_ms}ms")
        print(raw_text[:500])
        return False

    ok, reason = _validate(parsed)
    if not ok:
        print(f"FAIL (attempt {attempt_no}): {reason}, elapsed {elapsed_ms}ms")
        print(raw_text[:500])
        return False

    print(f"PASS: HTTP {status}, elapsed {elapsed_ms}ms")
    print(f"days: {json.dumps(parsed['days'])}")
    return True


def main() -> int:
    env = {**_load_env(REPO_ROOT / ".env"), **os.environ}
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    base_url = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic").strip()

    if not api_key:
        print("DEEPSEEK_API_KEY missing", file=sys.stderr)
        return 2

    if _attempt(api_key, base_url, 1):
        return 0
    print("Retrying once (max 2 attempts total)...")
    if _attempt(api_key, base_url, 2):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
