"""Merge selected keys into a .env file. Never prints values.

Stdin: JSON object of key -> value.
Arg 1: path to .env.

Only SERPAPI_KEY / DEEPSEEK_* / AMAP_KEY. Refuses MYSQL_* and CORS_ORIGINS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ALLOWED = {"SERPAPI_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "AMAP_KEY"}
BLOCKED_PREFIX = ("MYSQL", "CORS", "PATH", "HOME", "USER")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: merge_prod_env.py /path/to/.env", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    updates = json.load(sys.stdin)
    if not isinstance(updates, dict):
        print("stdin must be a JSON object", file=sys.stderr)
        return 2
    clean: dict[str, str] = {}
    for key, value in updates.items():
        if key not in ALLOWED or any(key.startswith(p) for p in BLOCKED_PREFIX):
            print(f"refusing key {key}", file=sys.stderr)
            return 2
        if not isinstance(value, str) or not value.strip():
            continue
        clean[key] = value.strip()
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 1
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key = raw.split("=", 1)[0].strip()
            if key in clean:
                out.append(f"{key}={clean[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in clean.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("updated", ",".join(sorted(clean)) or "(none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
