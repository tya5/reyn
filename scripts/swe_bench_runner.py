"""swe_bench_runner.py — SWE-bench harness wrapper for Reyn.

Reads a single SWE-bench instance JSON, delegates to ``reyn run swe_bench``,
and emits the harness-expected output shape on stdout.

Usage
-----
    python scripts/swe_bench_runner.py --input instance.json [--model-name reyn] [--timeout 600]
    python scripts/swe_bench_runner.py --stdin

Input JSON fields (standard SWE-bench format)
---------------------------------------------
    instance_id      str  — e.g. "django__django-1234"
    repo             str  — e.g. "django/django"
    base_commit      str  — e.g. "abc123..."
    problem_statement str
    hints_text       str  — optional
    test_patch       str  — optional

Output (one JSON object on stdout)
------------------------------------
Success::

    {"instance_id": "...", "model_name_or_path": "reyn", "model_patch": "<git diff>"}

Failure (reyn non-zero, timeout, or unparseable output) — wrapper still
exits 0 so the harness batch keeps going::

    {"instance_id": "...", "model_name_or_path": "reyn", "error": "..."}

All progress / diagnostic messages go to stderr only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

# Required keys every SWE-bench instance must carry.
_REQUIRED_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")


# ── pure helpers (testable without subprocess) ──────────────────────────────


def parse_input(text: str) -> dict[str, Any]:
    """Parse a JSON string into a SWE-bench instance dict.

    Raises
    ------
    ValueError
        If *text* is not valid JSON, or any required field is missing.
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object, got {type(obj).__name__}")

    missing = [f for f in _REQUIRED_FIELDS if not obj.get(f)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    return obj


def format_output(
    instance_id: str,
    model_name: str,
    *,
    patch: str | None = None,
    error: str | None = None,
) -> str:
    """Serialise one harness output record as a JSON line.

    Exactly one of *patch* or *error* must be provided.
    """
    if patch is not None and error is None:
        obj: dict[str, str] = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }
    elif error is not None and patch is None:
        obj = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "error": error,
        }
    else:
        raise ValueError("exactly one of patch or error must be supplied")

    return json.dumps(obj, ensure_ascii=False)


def extract_patch(reyn_stdout: str) -> str:
    """Extract the ``patch`` field from ``reyn run``'s JSON stdout.

    ``reyn run`` prints a block that includes ``=== Final Output ===`` followed
    by a JSON object on the following lines.  We scan for the JSON object and
    pull out ``data.patch`` (nested) or top-level ``patch``.

    Raises
    ------
    ValueError
        If the patch field cannot be found or the JSON is unparseable.
    """
    # Locate the JSON block that follows "=== Final Output ===" or any JSON
    # object containing a "patch" key.  We try two strategies:
    #
    # Strategy A: find the marker line and parse the block after it.
    # Strategy B: scan every line for a JSON object with a "patch" key.
    lines = reyn_stdout.splitlines()

    # Strategy A
    marker = "=== Final Output ==="
    for i, line in enumerate(lines):
        if marker in line:
            json_block = "\n".join(lines[i + 1 :]).strip()
            if json_block:
                try:
                    obj = json.loads(json_block)
                    return _extract_patch_from_obj(obj)
                except (json.JSONDecodeError, ValueError):
                    pass  # fall through to Strategy B
            break

    # Strategy B: scan every line
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            return _extract_patch_from_obj(obj)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError("could not find 'patch' field in reyn output")


def _extract_patch_from_obj(obj: Any) -> str:
    """Pull *patch* from a parsed JSON object (top-level or nested under data)."""
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")

    # Top-level "patch"
    if "patch" in obj:
        return str(obj["patch"])

    # Nested: {"data": {"patch": "..."}}
    data = obj.get("data")
    if isinstance(data, dict) and "patch" in data:
        return str(data["patch"])

    raise ValueError(f"no 'patch' key found in object; keys={list(obj.keys())}")


def run_reyn(
    instance: dict[str, Any],
    *,
    reyn_cmd: list[str] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """Shell out to ``reyn run swe_bench`` and return a result dict.

    Returns
    -------
    dict
        ``{"ok": True, "patch": "..."}`` on success, or
        ``{"ok": False, "error": "..."}`` on any failure.

    Parameters
    ----------
    reyn_cmd:
        Override the base command list.  Defaults to
        ``["reyn", "run", "swe_bench"]``.  Tests inject a fake script here.
    timeout:
        Subprocess wall-clock timeout in seconds.
    """
    cmd = reyn_cmd if reyn_cmd is not None else ["reyn", "run", "swe_bench"]
    input_json = json.dumps(instance, ensure_ascii=False)

    print(
        f"[swe_bench_runner] running: {' '.join(cmd)} (instance={instance['instance_id']})",
        file=sys.stderr,
    )

    try:
        proc = subprocess.run(
            [*cmd, input_json],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"reyn not found on PATH: {exc}"}
    except OSError as exc:
        return {"ok": False, "error": f"subprocess error: {exc}"}

    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or "")[:400]
        return {
            "ok": False,
            "error": f"reyn exited {proc.returncode}: {stderr_snippet}",
        }

    # Try to extract patch from stdout.
    try:
        patch = extract_patch(proc.stdout)
    except ValueError as exc:
        stdout_snippet = (proc.stdout or "")[:400]
        return {
            "ok": False,
            "error": f"could not parse reyn output: {exc}; stdout={stdout_snippet!r}",
        }

    return {"ok": True, "patch": patch}


# ── CLI entry point ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swe_bench_runner.py",
        description=(
            "Wrap `reyn run swe_bench` for the SWE-bench evaluation harness. "
            "Reads a single SWE-bench instance, runs the Reyn solver, and emits "
            "the harness-expected JSON on stdout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input", metavar="PATH",
        help="Path to a JSON file containing a single SWE-bench instance.",
    )
    source.add_argument(
        "--stdin", action="store_true",
        help="Read the SWE-bench instance JSON from stdin.",
    )

    p.add_argument(
        "--model-name", dest="model_name", default="reyn", metavar="NAME",
        help=(
            "Value for the harness 'model_name_or_path' field (default: reyn). "
            "Use a descriptive string so results are identifiable in harness output."
        ),
    )
    p.add_argument(
        "--timeout", type=int, default=600, metavar="SECONDS",
        help="Maximum seconds to wait for `reyn run` to complete (default: 600).",
    )
    p.add_argument(
        "--reyn-cmd", dest="reyn_cmd", default=None, metavar="CMD",
        help=(
            "Override the reyn invocation (space-separated).  "
            "Useful for testing with a local install: --reyn-cmd 'python -m reyn'."
        ),
    )

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns an integer exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # ── read input ────────────────────────────────────────────────────────────
    if args.stdin:
        raw = sys.stdin.read()
        source_label = "<stdin>"
    else:
        try:
            from pathlib import Path
            raw = Path(args.input).read_text(encoding="utf-8")
            source_label = args.input
        except OSError as exc:
            print(f"Error: cannot read input file: {exc}", file=sys.stderr)
            return 1

    # ── parse ─────────────────────────────────────────────────────────────────
    try:
        instance = parse_input(raw)
    except ValueError as exc:
        print(f"Error: invalid input ({source_label}): {exc}", file=sys.stderr)
        return 1

    instance_id = instance["instance_id"]

    # ── resolve reyn command ──────────────────────────────────────────────────
    if args.reyn_cmd:
        reyn_cmd = args.reyn_cmd.split()
    else:
        reyn_cmd = None  # run_reyn uses default ["reyn", "run", "swe_bench"]

    # ── run reyn ──────────────────────────────────────────────────────────────
    result = run_reyn(instance, reyn_cmd=reyn_cmd, timeout=args.timeout)

    # ── emit harness output ───────────────────────────────────────────────────
    if result["ok"]:
        line = format_output(instance_id, args.model_name, patch=result["patch"])
        print(line)
        print(
            f"[swe_bench_runner] done: {instance_id}",
            file=sys.stderr,
        )
    else:
        line = format_output(instance_id, args.model_name, error=result["error"])
        print(line)
        print(
            f"[swe_bench_runner] error: {instance_id}: {result['error']}",
            file=sys.stderr,
        )

    # Always exit 0 — the harness batch must continue on per-instance failures.
    return 0


if __name__ == "__main__":
    sys.exit(main())
