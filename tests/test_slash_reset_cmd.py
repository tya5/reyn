"""Tier 2: /reset slash — _format_currently_line helper + handler paths.

`_format_currently_line` builds a context line from `session.current_state_summary()`.
`reset_cmd` has four paths: no-confirm warning, confirm+no-registry error,
confirm+no-project-root error, confirm+valid-state success.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.slash.reset import _format_currently_line, reset_cmd
from reyn.runtime.outbox import OutboxMessage

# ── stubs ──────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, *, registry=None, summary_fn=None) -> None:
        self._registry = registry
        if summary_fn is not None:
            self.current_state_summary = summary_fn
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def system_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


class _FakeRegistry:
    def __init__(self, *, project_root=None) -> None:
        self._project_root = project_root


# ── _format_currently_line pure helper ────────────────────────────────────


def test_format_currently_no_summary_method_returns_empty() -> None:
    """Tier 2: session without current_state_summary → empty string."""
    session = object()  # no current_state_summary
    assert _format_currently_line(session) == ""


def test_format_currently_zero_skills() -> None:
    """Tier 2: zero running skills → 'Currently: 0 skills running.'"""
    session = _FakeSession(summary_fn=lambda: {"running_skills": 0})
    out = _format_currently_line(session)
    assert "Currently:" in out
    assert "0 skills running" in out


def test_format_currently_one_skill_singular() -> None:
    """Tier 2: exactly 1 running skill uses singular 'skill' not 'skills'."""
    session = _FakeSession(summary_fn=lambda: {"running_skills": 1})
    out = _format_currently_line(session)
    assert "1 skill running" in out
    assert "skills" not in out


def test_format_currently_multiple_skills_plural() -> None:
    """Tier 2: 3 running skills uses plural 'skills'."""
    session = _FakeSession(summary_fn=lambda: {"running_skills": 3})
    out = _format_currently_line(session)
    assert "3 skills running" in out


def test_format_currently_summary_raises_returns_empty() -> None:
    """Tier 2: if current_state_summary raises, _format_currently_line returns '' (best-effort)."""
    def _bad():
        raise RuntimeError("state unavailable")

    session = _FakeSession(summary_fn=_bad)
    assert _format_currently_line(session) == ""


# ── reset_cmd handler paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_no_confirm_sends_warning_not_error() -> None:
    """Tier 2: /reset without 'confirm' sends a warning system reply, not an error."""
    session = _FakeSession()
    await reset_cmd(session, "")
    assert session.system_text(), "expected warning reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_reset_no_confirm_warns_to_type_confirm() -> None:
    """Tier 2: the warning tells the user to type /reset confirm."""
    session = _FakeSession()
    await reset_cmd(session, "anything_but_confirm")
    assert "confirm" in session.system_text().lower()


@pytest.mark.asyncio
async def test_reset_confirm_no_registry_sends_error() -> None:
    """Tier 2: /reset confirm with no registry wired replies an error."""
    session = _FakeSession(registry=None)
    await reset_cmd(session, "confirm")
    assert session.error_text(), "expected error when no registry"
    assert not session.system_text()


@pytest.mark.asyncio
async def test_reset_confirm_no_project_root_sends_error() -> None:
    """Tier 2: /reset confirm with registry but no _project_root replies an error."""
    registry = _FakeRegistry(project_root=None)
    session = _FakeSession(registry=registry)
    await reset_cmd(session, "confirm")
    assert session.error_text(), "expected error when no _project_root"


@pytest.mark.asyncio
async def test_reset_confirm_valid_state_sends_success(tmp_path: Path) -> None:
    """Tier 2: /reset confirm with a valid project root sends a success reply."""
    registry = _FakeRegistry(project_root=tmp_path)
    session = _FakeSession(registry=registry)
    await reset_cmd(session, "confirm")
    text = session.system_text()
    assert text, f"expected success reply; got errors: {session.error_text()!r}"
    assert not session.error_text()
