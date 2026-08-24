#!/usr/bin/env python3
"""Fail when a PR body contains an open red blocking checkbox (#5135)."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_OPEN_BLOCK = re.compile(r"^\s*[-*+]\s*\[\s*\]\s*(?:\*\*\s*)*🔴", re.MULTILINE)


def evaluate_body(body: object) -> tuple[int, str]:
    """Return a failure for missing/non-string PR bodies or open blockers."""
    if not isinstance(body, str):
        return 2, "PR body could not be fetched"
    if _OPEN_BLOCK.search(body):
        return 1, "PR body contains an open blocking checkbox"
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


def run_gate(body_supplier: Callable[[], object]) -> int:
    """Evaluate a supplied PR body, keeping retrieval as an explicit seam."""
    try:
        body = body_supplier()
    except Exception as exc:  # noqa: BLE001 - the gate must fail closed
        print(f"PR body fetch failed: {exc}", file=sys.stderr)
        return 2
    code, message = evaluate_body(body)
    print(message)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int)
    group.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)
    if args.pr is not None:
        return run_gate(lambda: fetch_body(args.pr))
    return run_gate(
        lambda: json.loads(args.fixture.read_text(encoding="utf-8")).get("body")
    )


if __name__ == "__main__":
    raise SystemExit(main())
