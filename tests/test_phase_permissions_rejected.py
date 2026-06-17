"""Tests pinning the hard-rejection of phase-level permissions: blocks (ADR-0020).

Tier 2: OS invariant — phase.md frontmatter with a `permissions:` key MUST
raise ValueError at parse time, with a message pointing to skill.md.

No mocks, no private state.  Real parser only.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.core.compiler.parser import parse_phase


def _write_phase(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test_phase.md"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def test_phase_with_permissions_block_raises_at_parse_time(tmp_path: Path) -> None:
    """Tier 2: parse_phase raises ValueError when phase.md contains permissions:."""
    path = _write_phase(
        tmp_path,
        """\
        ---
        name: bad_phase
        input: some_artifact
        permissions:
          mcp:
            - fs
        ---
        Do something.
        """,
    )
    with pytest.raises(ValueError, match="phase-level 'permissions:'"):
        parse_phase(path)


def test_phase_without_permissions_block_parses_normally(tmp_path: Path) -> None:
    """Tier 2: parse_phase succeeds when phase.md has no permissions: key."""
    path = _write_phase(
        tmp_path,
        """\
        ---
        name: good_phase
        input: some_artifact
        ---
        Do something.
        """,
    )
    phase_def = parse_phase(path)
    assert phase_def.name == "good_phase"


def test_error_message_points_to_skill_md_migration(tmp_path: Path) -> None:
    """Tier 2: ValueError message contains 'skill.md frontmatter' migration hint."""
    path = _write_phase(
        tmp_path,
        """\
        ---
        name: migrating_phase
        permissions:
          shell: true
        ---
        Instructions.
        """,
    )
    with pytest.raises(ValueError, match="skill.md frontmatter"):
        parse_phase(path)
