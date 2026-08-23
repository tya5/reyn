#!/usr/bin/env python3
"""#5131 gate B — the "down" reactive framework never shrinks, and App's
imperative pushes into a widget never grow.

Gate A (``check_tui_widget_boundary.py``) is structural and zero-FP: an
import statement is unambiguous. Whether App→widget state flow actually
goes through ``reactive``/``watch_`` instead of an imperative call is
SEMANTIC — "did this migration duplicate state" is not something an AST
census can answer, and this gate does not claim to. It answers a narrower,
honest question instead: is the population of the two structural SIGNALS
moving the right direction?

  - ``reactive_count`` — every class-level ``reactive(...)`` assignment plus
    every ``watch_*``/``watch__*`` method, across every ``.py`` file in
    ``textual_chat/``. Ratchet FLOOR: must never DECREASE. A migration that
    adds a new reactive/watcher raises the floor for free; nothing has to
    be edited to let that count.
  - ``imperative_push_count`` — call sites in ``app.py`` (the one file Gate
    A lets touch widgets at all) shaped ``self.query_one(...).method(...)``
    — the App reaching into a widget it just looked up and calling ANY
    method on it directly. This is deliberately UNDER-selective, not
    over-precise: it counts every such call, not only the ones that push
    state (a pure-action call like ``.focus()`` counts the same as a
    state-push like ``.update_status(...)``) — distinguishing the two
    would be exactly the semantic "does this duplicate state" question
    Gate A already declined to answer, restated one level down. Counting
    the superset keeps the gate zero-FP at the cost of also flagging an
    unrelated new ``.focus()`` call — cheap to clear (``--write-baseline``
    + one PR line), and the baseline only grows when a human explicitly
    says why. Ratchet CEILING: must never INCREASE for free; a
    ``reactive``-based migration that REPLACES one of these sites makes
    this count drop for free, with nothing to edit to let that count.

Same baseline+``--write-baseline``+``--check-growth`` skeleton as
``mypy_ratchet.py``/``flat_tests_ratchet.py`` (architect/#3726 precedent):
a committed JSON baseline, re-measured live, red the moment either
direction regresses. ``--check-growth BASE_REF`` additionally rejects a
baseline edit that isn't backed by a real measured change (the same
"hand-editing the baseline to fake progress" defense those two scripts
already have).

NOT a duplication detector, NOT a proof of sufficiency — a floor and a
ceiling that stop this specific regression from being silent, nothing
more. See ``src/reyn/interfaces/CLAUDE.md``'s own #5131 section for the
4 rules this gate is a PARTIAL, structural witness for.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = _ROOT / "src" / "reyn" / "interfaces" / "inline" / "textual_chat"
_APP_PY = _PACKAGE_DIR / "app.py"
_BASELINE_PATH = _ROOT / "scripts" / "tui_reactive_ratchet_baseline.json"


def _is_reactive_call(node: ast.expr) -> bool:
    """True if *node* is a call to ``reactive(...)`` (bare name, since
    ``textual.reactive.reactive`` is always imported as the bare name in
    this codebase — matches the import shape ``check_tui_widget_boundary``
    itself assumes elsewhere)."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "reactive"


def _reactive_count_in_file(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.value is not None and _is_reactive_call(node.value):
            count += 1
        elif isinstance(node, ast.Assign) and _is_reactive_call(node.value):
            count += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("watch_"):
            count += 1
    return count


def measured_reactive_count(package_dir: Path = _PACKAGE_DIR) -> int:
    """Total reactive-declaration + watch-method count across every ``.py``
    file in *package_dir*. See the module docstring for exactly what
    counts."""
    return sum(_reactive_count_in_file(p) for p in sorted(package_dir.glob("*.py")))


def _is_query_one_call(node: ast.expr) -> bool:
    """True if *node* is ``self.query_one(...)`` — any argument shape."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query_one"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def measured_imperative_push_count(app_py: Path = _APP_PY) -> int:
    """Call sites in *app_py* shaped ``self.query_one(...).method(...)`` —
    App reaching into a widget it just looked up and calling a method on it
    directly. See the module docstring for why this is the CEILING signal."""
    if not app_py.is_file():
        return 0
    try:
        tree = ast.parse(app_py.read_text(encoding="utf-8"), filename=str(app_py))
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_query_one_call(node.func.value)
        ):
            count += 1
    return count


def _load_baseline(path: Path = _BASELINE_PATH) -> dict:
    if not path.is_file():
        return {"reactive_count": 0, "imperative_push_count": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_baseline(reactive_count: int, imperative_push_count: int, path: Path = _BASELINE_PATH) -> None:
    path.write_text(
        json.dumps(
            {"reactive_count": reactive_count, "imperative_push_count": imperative_push_count},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _git_show(base_ref: str, rel_path: str) -> "dict | None":
    try:
        raw = subprocess.run(
            ["git", "show", f"{base_ref}:{rel_path}"],
            cwd=_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline", action="store_true",
        help="Overwrite the committed baseline with today's measured counts. "
             "Legitimate for initial adoption or a real improvement — same "
             "warning as mypy_ratchet.py: this can also be used to silently "
             "paper over a regression, which is what --check-growth guards.",
    )
    parser.add_argument(
        "--check-growth", metavar="BASE_REF",
        help="Reject a baseline that moved in the WRONG direction relative "
             "to BASE_REF (reactive_count decreased, or imperative_push_count "
             "increased) without a matching real measured change.",
    )
    args = parser.parse_args()

    reactive_count = measured_reactive_count()
    imperative_push_count = measured_imperative_push_count()

    if args.write_baseline:
        _write_baseline(reactive_count, imperative_push_count)
        print(
            f"check_tui_reactive_ratchet: baseline written — "
            f"reactive_count={reactive_count}, imperative_push_count={imperative_push_count}"
        )
        return 0

    if args.check_growth:
        base_baseline = _git_show(args.check_growth, "scripts/tui_reactive_ratchet_baseline.json")
        if base_baseline is None:
            print(
                f"check_tui_reactive_ratchet: no baseline at {args.check_growth} — "
                "nothing to compare, treating as OK (initial adoption).",
            )
        else:
            current_baseline = _load_baseline()
            if current_baseline["reactive_count"] < base_baseline["reactive_count"]:
                print(
                    "check_tui_reactive_ratchet FAILED: baseline reactive_count "
                    f"dropped ({base_baseline['reactive_count']} -> "
                    f"{current_baseline['reactive_count']}) at {args.check_growth} — "
                    "a hand-edited baseline lowering the floor is exactly what "
                    "this gate exists to catch.",
                    file=sys.stderr,
                )
                return 1
            if current_baseline["imperative_push_count"] > base_baseline["imperative_push_count"]:
                print(
                    "check_tui_reactive_ratchet FAILED: baseline imperative_push_count "
                    f"rose ({base_baseline['imperative_push_count']} -> "
                    f"{current_baseline['imperative_push_count']}) at {args.check_growth} — "
                    "a hand-edited baseline raising the ceiling is exactly what "
                    "this gate exists to catch.",
                    file=sys.stderr,
                )
                return 1

    baseline = _load_baseline()
    failed = False
    if reactive_count < baseline["reactive_count"]:
        print(
            f"check_tui_reactive_ratchet FAILED: reactive_count dropped "
            f"({baseline['reactive_count']} -> {reactive_count}) — #5131's "
            "down-flow framework usage may not have shrunk on purpose. If "
            "this drop is real and intended, run --write-baseline.",
            file=sys.stderr,
        )
        failed = True
    if imperative_push_count > baseline["imperative_push_count"]:
        print(
            f"check_tui_reactive_ratchet FAILED: imperative_push_count rose "
            f"({baseline['imperative_push_count']} -> {imperative_push_count}) — "
            "a new self.query_one(...).method(...) site in app.py pushes "
            "state into a widget imperatively instead of through a "
            "reactive/watch_ pair (#5131). If this is genuinely necessary, "
            "run --write-baseline and say why in the PR.",
            file=sys.stderr,
        )
        failed = True

    if failed:
        return 1

    print(
        f"check_tui_reactive_ratchet OK: reactive_count={reactive_count} "
        f"(floor {baseline['reactive_count']}), "
        f"imperative_push_count={imperative_push_count} "
        f"(ceiling {baseline['imperative_push_count']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
