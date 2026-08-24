#!/usr/bin/env python3
"""Fail when a PR body contains an open red blocking checkbox (#5135)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_OPEN_BLOCK = "- [ ] 🔴"


def evaluate_body(body: object) -> tuple[int, str]:
    """Return a failure for missing/non-string PR bodies or open blockers."""
    if not isinstance(body, str):
        return 2, "PR body could not be fetched"
    if _OPEN_BLOCK in body:
        return 1, "PR body contains an open blocking checkbox (- [ ] 🔴)"
    return 0, "PR body has no open blocking checkbox"


def fetch_body(pr: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", "body"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    body = data.get("body")
    if not isinstance(body, str):
        raise ValueError("PR body is missing or not a string")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int)
    group.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)
    try:
        body = fetch_body(args.pr) if args.pr is not None else json.loads(
            args.fixture.read_text(encoding="utf-8")
        ).get("body")
        code, message = evaluate_body(body)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"PR body fetch failed: {exc}", file=sys.stderr)
        return 2
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
