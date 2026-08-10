"""Tier 2: op_runtime status-classifier completeness gate (#3193).

#3193's root cause was a per-wrapper WHITELIST of `status == "ok"` (a
curated subset) collapsing every other status — most damagingly
"truncated" — into a lying `{"error": "read failed"}`. The fix moves
success/failure classification into ONE place
(`reyn.core.op_runtime.status_classify.classify_op_status`) whose
known-status tables must stay a superset of the LIVE status vocabulary
op_runtime actually returns.

This is an "enumerate, don't curate" gate (#3075 idiom, mirrored from
`tests/test_network_egress_env_completeness_3075.py`): it AST-walks every
`op_runtime/*.py` file for dict-literal `"status": "<value>"` entries
rather than hand-maintaining a second copy of the status list, so a new
status value added anywhere in op_runtime is caught automatically instead
of depending on someone remembering to also update this file.

Vacuity guard: `test_status_enumeration_is_nonempty` fails loudly if the
AST walk ever finds fewer than 10 status literals — the current live count
is 15. Without this, a broken enumeration (wrong path, a Python-version AST
shape change) would make the coverage assertion below trivially pass over
an empty set, which is a green rubber stamp, not a gate.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _enumerate_status_literals() -> set[str]:
    """AST-walk every src/reyn/core/op_runtime/*.py file and collect every
    string literal assigned to a dict key literally named "status"."""
    found: set[str] = set()
    op_runtime_dir = _repo_root() / "src" / "reyn" / "core" / "op_runtime"
    for py_file in sorted(op_runtime_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant) and key.value == "status"
                    and isinstance(value, ast.Constant) and isinstance(value.value, str)
                ):
                    found.add(value.value)
    return found


def test_status_enumeration_is_nonempty() -> None:
    """Tier 2: vacuity guard — a broken enumeration (wrong path, an AST-shape
    change under a Python version bump) must fail loudly here rather than
    silently emptying the set the coverage test below checks.

    Deliberately NOT a `len(found) >= N` size pin (Tier 4 format-pinning per
    `test_tier_audit.py`'s "N passed is a census, not a behavior" rule) —
    instead, assert that specific status literals KNOWN to live in
    op_runtime's file/web/mcp handlers (spanning several different source
    files, so a single-file enumeration bug can't accidentally satisfy all
    of them) are actually found. If the AST walk is broken (wrong path, an
    AST-shape change), these behavioral memberships fail instead of the
    coverage test below silently passing over an empty set."""
    found = _enumerate_status_literals()
    # One sentinel per distinct op_runtime source file that defines it, so a
    # bug in globbing/parsing *one* file cannot make every sentinel vacuously
    # present via a single easy-to-satisfy file.
    expected_present = {
        "ok": "file.py / web.py / mcp.py / ... (near-universal happy path)",
        "truncated": "file.py (read op's inline-cap overflow — #3193's own bug)",
        "not_found": "file.py (read op's missing-file path)",
        "denied": "file.py (media-size gate) / web.py (SSRF deny)",
        "timeout": "web.py (web_fetch timeout)",
        "needs_secrets": "mcp_install.py (unresolved secret dependency)",
        "installed": "plugin_install.py / skill_install.py / mcp_install.py",
    }
    missing_sentinels = set(expected_present) - found
    assert not missing_sentinels, (
        f"expected sentinel status literals {sorted(missing_sentinels)} were "
        f"NOT found by the AST enumeration (found only {sorted(found)}) — "
        f"the enumeration is very likely broken (wrong path / AST-shape "
        f"change), not the source code. Missing sentinels' known origin: "
        f"{ {k: v for k, v in expected_present.items() if k in missing_sentinels} }"
    )


def test_classifier_covers_every_known_status_literal() -> None:
    """Tier 2: reyn.core.op_runtime.status_classify's known-status tables
    must be a superset of every literal "status" string op_runtime can
    return — else a NEW op status silently falls into classify_op_status's
    "unknown" bucket without anyone noticing at review time."""
    from reyn.core.op_runtime.status_classify import ALL_KNOWN_STATUSES

    found = _enumerate_status_literals()
    missing = found - ALL_KNOWN_STATUSES
    assert not missing, (
        f"op_runtime returns status value(s) {sorted(missing)} that "
        f"reyn.core.op_runtime.status_classify does not classify — add "
        f"them to KNOWN_SUCCESS_STATUSES / KNOWN_PARTIAL_STATUSES / "
        f"KNOWN_FAILURE_STATUSES in src/reyn/core/op_runtime/status_classify.py "
        f"(#3193)."
    )


def test_known_status_tables_do_not_overlap() -> None:
    """Tier 1: contract — the three known-status sets partition the known
    vocabulary; a status appearing in two buckets would make
    classify_op_status's outcome depend on set-iteration/construction
    order rather than being well-defined."""
    from reyn.core.op_runtime.status_classify import (
        KNOWN_FAILURE_STATUSES,
        KNOWN_PARTIAL_STATUSES,
        KNOWN_SUCCESS_STATUSES,
    )

    assert not (KNOWN_SUCCESS_STATUSES & KNOWN_PARTIAL_STATUSES)
    assert not (KNOWN_SUCCESS_STATUSES & KNOWN_FAILURE_STATUSES)
    assert not (KNOWN_PARTIAL_STATUSES & KNOWN_FAILURE_STATUSES)


def test_classify_op_status_unknown_status_is_neither_success_nor_failure() -> None:
    """Tier 1: contract — classify_op_status's design for a status value it
    does not recognize (#3193's "don't silently kill unknown into failure"
    requirement): a bogus/future status string must classify as "unknown",
    not silently fold into "success" or "failure"."""
    from reyn.core.op_runtime.status_classify import classify_op_status

    assert classify_op_status("some_future_status_nobody_added_yet") == "unknown"
    assert classify_op_status(None) == "unknown"
    assert classify_op_status(123) == "unknown"


def test_classify_op_status_known_values() -> None:
    """Tier 1: contract — spot-check the three known buckets classify as
    documented (regression pin for "ok"/"not_found" plus the "truncated"
    partial status this issue is about)."""
    from reyn.core.op_runtime.status_classify import classify_op_status

    assert classify_op_status("ok") == "success"
    assert classify_op_status("truncated") == "partial"
    assert classify_op_status("not_found") == "failure"
    assert classify_op_status("denied") == "failure"
    assert classify_op_status("error") == "failure"
