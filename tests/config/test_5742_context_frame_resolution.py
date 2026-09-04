"""Tier 1: #5742 — ``resolve_context_text``, the shared resolve+read+
classify+warn+emit implementation the project frame (``resolve_project_
context``) and the agent frame (``RouterHostAdapter._read_agent_
instructions``) both call — one implementation, exercised here directly
against real files on disk, no mocks.

Real ``.reyn/events/direct/config/*.jsonl`` readback for the audit-event
assertions (mirrors ``tests/config/test_project_context_agents_md.py``'s
own no-mock-events idiom) — never a patched ``emit_direct_event``.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.config.loader import (
    PROJECT_CONTEXT_DISABLED,
    PROJECT_CONTEXT_NO_CANDIDATE,
    PROJECT_CONTEXT_OK,
    PROJECT_CONTEXT_UNREADABLE,
    resolve_context_text,
)


def _read_direct_events(reyn_root: Path, kind: str) -> list[dict]:
    direct_dir = reyn_root / "events" / "direct" / "config"
    if not direct_dir.is_dir():
        return []
    out: list[dict] = []
    for path in direct_dir.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == kind:
                out.append(row)
    return out


def test_unset_with_no_default_file_is_no_candidate_not_unreadable(tmp_path: Path) -> None:
    """Tier 1: an UNSET config (``rel=None``) with neither ``REYN.md`` nor
    ``AGENTS.md`` present is the silent, normal "nothing was ever asked
    for" case — ``PROJECT_CONTEXT_NO_CANDIDATE``, never ``UNREADABLE``
    (lead-coder's own wording: a typo'd EXPLICIT pin is "指定したが読めな
    い"; an unset value with nothing found is "候補が無い" — two different
    outcomes this function must not collapse)."""
    reyn_root = tmp_path / ".reyn"
    text, path, outcome = resolve_context_text(
        None, tmp_path, reyn_root=reyn_root, scope="project",
    )
    assert (text, path, outcome) == ("", None, PROJECT_CONTEXT_NO_CANDIDATE)
    assert _read_direct_events(reyn_root, "project_context_unreadable") == []


def test_explicit_empty_string_is_disabled(tmp_path: Path) -> None:
    """Tier 1: ``rel=""`` (an operator's explicit opt-out) is ``DISABLED``
    — distinct from both ``NO_CANDIDATE`` (nothing was ever configured)
    and ``UNREADABLE`` (a real name was given)."""
    (tmp_path / "REYN.md").write_text("hello", encoding="utf-8")
    text, path, outcome = resolve_context_text(
        "", tmp_path, reyn_root=tmp_path / ".reyn", scope="project",
    )
    assert (text, path, outcome) == ("", None, PROJECT_CONTEXT_DISABLED)


def test_explicit_pin_to_a_nonexistent_file_is_unreadable_not_no_candidate(
    tmp_path: Path, caplog,
) -> None:
    """Tier 1: strip-falsifier target — an EXPLICIT pin naming a file that
    does not exist at all must classify as ``UNREADABLE`` (lead-coder's
    own literal wording lists a typo'd path as an example of "指定したが
    読めない", not "候補が無い"). A real WARN log line and a real
    ``project_context_unreadable`` audit-event on disk are BOTH required
    — this is the runtime-side half of #5742's "捏造しないこと"
    requirement, not just a return-value shape."""
    import logging

    reyn_root = tmp_path / ".reyn"
    reyn_root.mkdir()
    with caplog.at_level(logging.WARNING):
        text, path, outcome = resolve_context_text(
            "NOPE.md", tmp_path, reyn_root=reyn_root, scope="project",
        )
    assert (text, path, outcome) == ("", None, PROJECT_CONTEXT_UNREADABLE)
    assert any("NOPE.md" in r.message for r in caplog.records), (
        f"expected a WARN naming the missing pin; got {[r.message for r in caplog.records]!r}"
    )
    events = _read_direct_events(reyn_root, "project_context_unreadable")
    assert any(
        e["data"]["scope"] == "project" and e["data"]["path"] == "NOPE.md"
        for e in events
    ), f"expected a matching project_context_unreadable event on disk; got {events!r}"


def test_agent_scope_unreadable_stamps_scope_agent_not_project(
    tmp_path: Path,
) -> None:
    """Tier 1: the SAME shared function, called with ``scope="agent"`` (the
    shape ``RouterHostAdapter._read_agent_instructions`` uses) — the
    emitted event carries ``scope="agent"``, never ``"project"``. This is
    the concrete witness for #5742's "枠を field で運ぶ、path の形から推
    測させない" requirement: two calls differing ONLY in ``scope=`` must
    produce events a consumer can tell apart without inspecting ``path``."""
    reyn_root = tmp_path / ".reyn"
    agent_dir = tmp_path / ".reyn" / "agents" / "coder1"
    agent_dir.mkdir(parents=True)
    reyn_root.mkdir(exist_ok=True)

    resolve_context_text(
        "MISSING.md", agent_dir, reyn_root=reyn_root, scope="agent",
    )
    events = _read_direct_events(reyn_root, "project_context_unreadable")
    assert any(e["data"]["scope"] == "agent" for e in events), (
        f"expected a project_context_unreadable event stamped scope=agent; got {events!r}"
    )


def test_a_real_read_error_after_resolution_is_also_unreadable(
    tmp_path: Path,
) -> None:
    """Tier 1: strip-falsifier — a candidate that resolves (passes
    ``is_file()``) but raises ``OSError`` on ``read_text`` (e.g. a
    permission failure, or — as exercised here — a directory swapped in
    place of the expected file's sibling after the initial ``is_file()``
    check is not reproducible portably, so this test uses an
    unreadable-permissions file directly) is UNREADABLE, with ``path`` set
    to the real resolved :class:`~pathlib.Path` (not just the configured
    string) — the ``OSError`` branch is a DIFFERENT code path from the
    does-not-exist-at-all branch above, and both must land on the same
    outcome."""
    import os
    import stat

    target = tmp_path / "REYN.md"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o000)
    try:
        if os.access(target, os.R_OK):
            import pytest

            pytest.skip("running as a user that bypasses file permissions (e.g. root)")
        text, path, outcome = resolve_context_text(
            None, tmp_path, reyn_root=tmp_path / ".reyn", scope="project",
        )
        assert outcome == PROJECT_CONTEXT_UNREADABLE
        assert path == target
        assert text == ""
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_ok_outcome_with_default_order_prefers_reyn_md(tmp_path: Path) -> None:
    """Tier 1: regression witness for the #5742 default-order flip, at the
    shared-resolver layer (the project-frame-specific witness already
    lives in ``tests/config/test_project_context_agents_md.py`` — this
    one exercises the SAME flip through the frame-agnostic function the
    agent side also depends on, so the flip is proven once at its actual
    source, not just at one of its two callers)."""
    (tmp_path / "REYN.md").write_text("reyn content", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents content", encoding="utf-8")
    text, path, outcome = resolve_context_text(
        None, tmp_path, reyn_root=tmp_path / ".reyn", scope="agent",
    )
    assert outcome == PROJECT_CONTEXT_OK
    assert text == "reyn content"
    assert path == tmp_path / "REYN.md"


def test_strip_false_preserves_trailing_whitespace_for_the_agent_frame(
    tmp_path: Path,
) -> None:
    """Tier 1: strip-falsifier for the accidental-stripping regression
    caught during authoring — ``get_project_context()``'s own docstring
    forbids stripping the agent-side text ("stripping it would silently
    change what an operator's REYN.md ... renders", byte-identical
    claim). ``strip=False`` must return the RAW file content, trailing
    newline and all; the default (``strip=True``, the project frame's own
    pre-#5742 contract) must still strip."""
    (tmp_path / "REYN.md").write_text("hello\n\n", encoding="utf-8")

    raw_text, _p, _o = resolve_context_text(
        None, tmp_path, reyn_root=tmp_path / ".reyn", scope="agent", strip=False,
    )
    assert raw_text == "hello\n\n"

    stripped_text, _p2, _o2 = resolve_context_text(
        None, tmp_path, reyn_root=tmp_path / ".reyn", scope="project", strip=True,
    )
    assert stripped_text == "hello"


def test_two_consecutive_resolutions_with_no_file_change_return_the_same_text(
    tmp_path: Path,
) -> None:
    """Tier 1: condition A's own regression witness (architect's explicit
    acceptance item — "already satisfied, zero work required" but pinned
    here so a future change cannot silently reverse it). Two calls
    against an unchanged file must return byte-identical text — this is
    the property the system-prompt cache-hit behaviour depends on
    (``get_project_context()``'s own docstring, #4830)."""
    (tmp_path / "REYN.md").write_text("stable content\n", encoding="utf-8")
    first, _p1, _o1 = resolve_context_text(
        None, tmp_path, reyn_root=tmp_path / ".reyn", scope="agent", strip=False,
    )
    second, _p2, _o2 = resolve_context_text(
        None, tmp_path, reyn_root=tmp_path / ".reyn", scope="agent", strip=False,
    )
    assert first == second == "stable content\n"
