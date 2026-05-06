"""Tier 1 (framework boundary): regression tests for skill-name resolution.

Pins the contract introduced when `resolve_skill_path` was changed to raise
`SkillNotFoundError` instead of calling `sys.exit(1)`. The latter would leak a
`SystemExit` (a `BaseException`, not `Exception`) past the op-runtime's
`except Exception` guard, aborting the eval CLI mid-iteration whenever a
target skill couldn't be resolved.

Tier classification: these tests pin the public exception type contract of
``resolve_skill_path`` (= a single function's API surface). They are
contract tests, not OS invariants — hence Tier 1.
"""
from __future__ import annotations

import pytest

from reyn.skill.skill_paths import (
    SkillNotFoundError,
    resolve_skill_path,
)


def test_resolve_missing_skill_raises_not_found(tmp_path, monkeypatch):
    """Tier 1: missing skill -> SkillNotFoundError (NOT SystemExit)."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SkillNotFoundError) as exc_info:
        resolve_skill_path("definitely_not_a_real_skill_xyz_999")
    msg = str(exc_info.value)
    assert "definitely_not_a_real_skill_xyz_999" in msg


def test_skill_not_found_is_caught_by_except_exception(tmp_path, monkeypatch):
    """Tier 1: SkillNotFoundError must inherit from Exception so the
    op-runtime's generic `except Exception` clause turns it into
    status='error' rather than letting it escape the loop."""
    monkeypatch.chdir(tmp_path)
    try:
        resolve_skill_path("missing_skill_abc")
    except Exception as exc:
        assert isinstance(exc, SkillNotFoundError)
        assert isinstance(exc, FileNotFoundError)
        return
    pytest.fail("Expected SkillNotFoundError to be caught by `except Exception`")


def test_skill_not_found_is_not_system_exit(tmp_path, monkeypatch):
    """Tier 1: direct contract — must not raise SystemExit. Without this,
    the eval CLI's `_run_case` (`except Exception`) misses the failure
    and the whole process exits mid-iteration on the first per-case
    run_skill miss."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SkillNotFoundError):
        resolve_skill_path("another_missing_skill")
    # Negative assertion: prior implementation called sys.exit(1) which
    # would surface as SystemExit, which pytest.raises(Exception) misses.
    with pytest.raises(Exception):
        resolve_skill_path("yet_another_missing_skill")
