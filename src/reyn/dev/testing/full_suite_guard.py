"""Mechanically stop a local pytest run that collected a full-or-near-full
suite (#5850) — CI's own full run is unaffected.

WHY THIS EXISTS
    "Run pytest scoped to your diff, not the full suite locally" is stated
    three separate places (CLAUDE.md's own PR-workflow section, every
    session's own brief, and the coding session's own acknowledgment of
    that brief) and was broken twice by different sessions the SAME night
    (2026-09-06), each one honestly believing what they had just typed
    was "scoped" — a diff can span many files after a real refactor, and
    counting files (not collected test items) does not catch that. The
    owner's own development machine ran out of memory running two
    full-shaped local suites at once and had to force-kill three
    sessions (e2e-coder / tui-coder / architect) to recover it.

    CLAUDE.md's own editing rule: "If CI can catch the violation, write
    the gate, not a rule here." A broadcast, a written brief, and an
    honest ack are all still WORDS — none of them can be strip-falsified,
    and each was independently broken. This is the strip-falsifiable
    version: a session that removes/bypasses this file (or reverts this
    change) makes the incident's own recorded collection counts pass
    again, immediately, in this repo's own test suite.

THE NUMBER (measured, #5850's own incident data)
    Every LOCAL run this incident's own `.pytest_cache` measurement
    recovered was one of two shapes: a genuinely SCOPED run (a handful of
    explicit files — 26 to 106 collected items across every scoped run
    measured) or a wide "sweep" a session ran anyway, believing it was
    still scoped (1,311 / 3,218 / 6,200 items post-ban; 8,203 / 8,177
    pre-ban, from a "run the identical command against main" instruction
    that itself produced a near-full collection). The full suite CI runs
    is ~13.6k items. :data:`FULL_SUITE_COLLECTION_CEILING` (300) sits
    strictly above every real scoped number this incident's own data
    shows and strictly below every real sweep/full number — the boundary
    a genuinely diff-scoped run can never accidentally cross, and a
    "just a few more files" sweep can only cross on purpose, by setting
    the override below first.

THE OVERRIDE
    One explicit, visible env var: :data:`OVERRIDE_ENV_VAR`
    (``REYN_FULL_SUITE_OK=1``) — never a silent path through this gate.
    Matches the SAME override name ``block_wide_pytest.sh`` (the
    Claude-Code-harness-side gate for a bare/directory-shaped `pytest`
    invocation) already uses, so a session that has learned one override
    name has learned both.

WHY A COLLECTION-COUNT GATE, NOT JUST THE EXISTING SHAPE-BASED ONE
    ``block_wide_pytest.sh`` blocks a bare/directory-shaped command
    (``pytest`` with no args, ``pytest tests/``) — a SHAPE check, blind
    to a command that lists many EXPLICIT files (e.g. every file touched
    by a large refactor) and still collects thousands of items. It also
    lives in the Claude Code harness, one specific environment — this
    gate lives in the repo's OWN ``tests/conftest.py``, so it fires for
    ANY local invocation regardless of which environment ran it. The two
    are complementary, not redundant: one catches the command's SHAPE,
    this one catches what that command actually COLLECTED.

WHY CI IS UNCONDITIONALLY EXEMPT
    CI's own full run (``CI`` / ``GITHUB_ACTIONS`` env vars, set by
    GitHub Actions itself, never by a local shell) is *supposed* to
    collect everything — that is its entire job. This gate exists to
    stop a LOCAL developer machine from doing CI's job on CI's own
    memory budget.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

#: See "THE NUMBER" above for the measured justification.
FULL_SUITE_COLLECTION_CEILING = 300

#: See "THE OVERRIDE" above — the ONE escape hatch, explicit and visible
#: in the command itself, never inferred.
OVERRIDE_ENV_VAR = "REYN_FULL_SUITE_OK"

#: Both are set by GitHub Actions itself (never by a local shell) — CI's
#: own full run is unconditionally exempt, see "WHY CI IS UNCONDITIONALLY
#: EXEMPT" above.
_CI_ENV_VARS = ("CI", "GITHUB_ACTIONS")


def _is_ci() -> bool:
    return any(os.environ.get(name) for name in _CI_ENV_VARS)


def _override_is_set() -> bool:
    return bool(os.environ.get(OVERRIDE_ENV_VAR))


def pytest_collection_modifyitems(
    session: "pytest.Session", config: "pytest.Config", items: "list[pytest.Item]",
) -> None:
    """#5850: abort the SESSION (not just this one file) the moment
    collection produces more items than a genuinely diff-scoped local run
    ever legitimately would. Runs after collection, before any test
    executes — the memory this exists to protect is never spent running
    tests that were about to be aborted anyway.

    ``pytest.exit`` (not a bare ``sys.exit``/raise) is pytest's own
    session-abort primitive — it prints the message given, sets the exit
    code, and unwinds cleanly without running a single test."""
    if _is_ci() or _override_is_set():
        return
    if len(items) <= FULL_SUITE_COLLECTION_CEILING:
        return
    import pytest

    pytest.exit(
        f"BLOCKED by tests/conftest.py's #5850 collection guard: this "
        f"local run collected {len(items)} tests, over the "
        f"{FULL_SUITE_COLLECTION_CEILING}-item ceiling for a local run "
        f"(CLAUDE.md: local pytest is scoped to your diff; the full suite "
        f"runs in CI). Run pytest against EXPLICIT test file(s), not a "
        f"wide sweep or a directory. If a wider local run is genuinely "
        f"required, ask lead-coder first, then set "
        f"{OVERRIDE_ENV_VAR}=1 (visible in the command) to override this "
        f"once.",
        returncode=1,
    )
