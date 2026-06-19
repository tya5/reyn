"""Tier 2: skill_runner.spawn uses the OS canonical run_id form.

tui-coder finding #1 cross-layer (2026-05-28): prior to this fix,
`skill_runner.spawn` constructed its own run_id with a `_4-hex` suffix
while `agent._make_run_id` produced a sibling form with no suffix.
The same skill run instance ended up with TWO different run_id forms
in flight at different layers — TUI `remove_async_task(run_id)` then
failed to find rows by key, leaving stuck rows.

Root cause class: same wiring-gap pattern as PR #1004 (Tool to
OpContext bridge) — "declared OS-level canonical not honored at a
downstream construction site". This fix funnels both spawn paths
through `SkillRuntime._make_run_id` to eliminate the mismatch class.

This file pins:
  1. `SkillRuntime._make_run_id` produces a parseable form with microsecond
     precision + 4-hex suffix.
  2. Concurrent calls do not collide (= 100 same-microsecond same-
     skill calls produce 100 distinct run_ids).
  3. All `skill_runner.spawn` paths use `SkillRuntime._make_run_id` rather
     than constructing their own (= source-level audit, structural
     guard against future re-introduction of the bug).

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning (= we check
membership / uniqueness / count properties, not exact strings).
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.skill.skill_runtime import SkillRuntime

# Canonical run_id form: YYYYMMDDTHHMMSSffffffZ_<skill_name>_<4hex>
# - timestamp: 14 base digits + 6 microsecond digits + Z = 21 chars
# - underscore + skill_name (safe-name, alphanumeric/_/-)
# - underscore + 4 lowercase hex chars
_CANONICAL_RUN_ID_PATTERN = re.compile(
    r"^\d{8}T\d{6}\d{6}Z_[A-Za-z0-9_\-]+_[0-9a-f]{4}$"
)


def test_make_run_id_matches_canonical_form() -> None:
    """Tier 2: SkillRuntime._make_run_id returns the canonical YYYYMMDDTHHMMSSffffffZ_<name>_<4hex> form."""
    run_id = SkillRuntime._make_run_id("test_skill")
    assert _CANONICAL_RUN_ID_PATTERN.match(run_id), (
        f"SkillRuntime._make_run_id returned {run_id!r}; expected canonical "
        f"form `YYYYMMDDTHHMMSSffffffZ_<safe_name>_<4hex>`."
    )


def test_make_run_id_includes_safe_skill_name() -> None:
    """Tier 2: skill name embedded in run_id is the safe form (= no slashes/spaces)."""
    run_id = SkillRuntime._make_run_id("my/skill with spaces")
    # The canonical form preserves the skill-name segment between the two
    # underscores. Safe-name replaces non-alphanumeric runs with single _.
    assert "/" not in run_id and " " not in run_id, (
        f"run_id {run_id!r} contains unsafe characters (path / space). "
        f"The safe-name transformation must strip them."
    )


def test_make_run_id_concurrent_calls_are_unique() -> None:
    """Tier 2: 100 successive calls produce 100 distinct run_ids.

    Microsecond timestamp + 4-hex suffix together prevent collision
    even when calls fire as fast as the interpreter can issue them.
    Pre-fix (= no microseconds + no suffix), 100 same-second calls
    would all share one run_id.
    """
    ids = {SkillRuntime._make_run_id("test_skill") for _ in range(100)}
    n_unique = len(ids)
    assert n_unique == 100, (
        f"SkillRuntime._make_run_id collided: 100 calls produced {n_unique} "
        f"distinct run_ids. Microsecond + 4-hex suffix should be unique."
    )


def test_skill_runner_does_not_construct_own_run_id() -> None:
    """Tier 2: source-level audit — skill_runner.py uses SkillRuntime._make_run_id, not its own.

    Catches future re-introduction of the bespoke
    `datetime.now(...).strftime('%Y%m%dT%H%M%SZ')` + `uuid.uuid4().hex[:4]`
    construction in skill_runner.py. The structural fix funneled both
    spawn paths through `SkillRuntime._make_run_id`; this test pins that
    invariant against accidental reversal.
    """
    source = (
        Path(__file__).parent.parent / "src" / "reyn" / "runtime" / "services"
        / "skill_runner.py"
    ).read_text(encoding="utf-8")
    # The old bespoke construction signature: a strftime call that
    # generates the timestamp portion of a run_id, paired with
    # `uuid.uuid4().hex[:4]` for the suffix, both inside an f-string.
    # If both patterns appear together in skill_runner.py, that's the
    # pre-fix construction shape.
    has_strftime_date = "strftime('%Y%m%dT%H%M%SZ')" in source
    has_hex_4_suffix = "uuid.uuid4().hex[:4]" in source
    assert not (has_strftime_date and has_hex_4_suffix), (
        "skill_runner.py contains both the bespoke `strftime('%Y%m%dT%H%M%SZ')` "
        "+ `uuid.uuid4().hex[:4]` construction — that is the pre-fix shape "
        "and re-introduces the cross-layer run_id form mismatch (tui-coder "
        "finding #1, 2026-05-28). Use `SkillRuntime._make_run_id(skill_name)` "
        "instead so the canonical OS-level form is used everywhere."
    )


def test_skill_runner_references_canonical_constructor() -> None:
    """Tier 2: source-level audit — skill_runner.py imports/calls SkillRuntime._make_run_id.

    Positive companion to the negative audit above. Pins the structural
    fix: at least one occurrence of `SkillRuntime._make_run_id` (with optional
    aliased name like `_SkillRuntime`) appears in the file.
    """
    source = (
        Path(__file__).parent.parent / "src" / "reyn" / "runtime" / "services"
        / "skill_runner.py"
    ).read_text(encoding="utf-8")
    references_canonical = (
        "SkillRuntime._make_run_id" in source or "_SkillRuntime._make_run_id" in source
    )
    assert references_canonical, (
        "skill_runner.py must reference `SkillRuntime._make_run_id` (the OS-level "
        "canonical run_id constructor). Bespoke construction in skill_runner "
        "re-introduces the cross-layer mismatch class (tui-coder finding #1)."
    )
