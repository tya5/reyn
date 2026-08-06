"""Tier 2: /memory slash — dispatch paths, including the data-store reads (#3721).

The 'list' and 'view' subcommand paths call into ``reyn.data.memory``, which
needs a real filesystem — covered below via a real ``session.workspace_dir``
and real memory files under ``tmp_path``, exactly the shape #3721's fix
resolves through (``ctx.transport.project_root()``, never ambient cwd). The
no-args / unknown-sub paths short-circuit before touching the store at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.slash.memory import memory_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session._outbox)


class _FakeSession:
    def __init__(self) -> None:
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


@pytest.mark.asyncio
async def test_memory_no_args_shows_usage() -> None:
    """Tier 2: /memory with no args → usage hint (not an error)."""
    session = _FakeSession()
    await memory_cmd(_ctx(session), "")  # type: ignore[arg-type]
    text = session.reply_text()
    assert "list" in text.lower()
    assert "view" in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_memory_unknown_sub_replies_error() -> None:
    """Tier 2: /memory with an unrecognised sub-command → usage error."""
    session = _FakeSession()
    await memory_cmd(_ctx(session), "delete foo")  # type: ignore[arg-type]
    assert session.error_text()
    assert not session.reply_text()


class _FakeSessionWithWorkspace(_FakeSession):
    """A session carrying a real ``workspace_dir`` — what
    ``RecordingTransport.project_root()`` (#3721) derives the memory root
    from, mirroring production's ``InProcessTransport``/``SessionBoundTransport``."""

    def __init__(self, workspace_dir: Path) -> None:
        super().__init__()
        self.workspace_dir = workspace_dir


def _write_entry(mem_dir: Path, filename: str, *, name: str, type_: str, description: str = "") -> None:
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / filename).write_text(
        f'---\nname: {name}\ndescription: "{description}"\n'
        f"metadata:\n  type: {type_}\n---\n\nBody for {name}.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_memory_list_reads_through_the_resolved_project_root(tmp_path: Path) -> None:
    """Tier 2: /memory list reads real entries via ctx.transport.project_root()
    — never ambient cwd (#3721's own fix)."""
    workspace_dir = tmp_path / ".reyn" / "agents" / "test"
    _write_entry(tmp_path / ".reyn" / "memory", "user_role.md", name="user-role", type_="user")
    session = _FakeSessionWithWorkspace(workspace_dir)

    await memory_cmd(_ctx(session), "list")

    text = session.reply_text()
    assert "user-role" in text
    assert not session.error_text()


@pytest.mark.asyncio
async def test_memory_list_with_no_entries_says_none_not_unresolved(tmp_path: Path) -> None:
    """Tier 2: FALSIFY the two-answers condition — a resolvable but EMPTY
    project root says "no memory entries yet", never the root-unresolved
    message; these are different answers to different questions (owner's
    own fix condition on #3721)."""
    session = _FakeSessionWithWorkspace(tmp_path / ".reyn" / "agents" / "test")

    await memory_cmd(_ctx(session), "list")

    text = session.reply_text()
    assert "no memory entries" in text.lower()
    assert "connection" not in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_memory_list_with_no_session_reports_root_unresolved_distinctly() -> None:
    """Tier 2: FALSIFY — no session (project_root() → None, the genuinely-remote
    shape) reports the distinct "can't determine" message, never silently reads
    as "0 memory entries"."""
    ctx = slash_ctx(None)

    await memory_cmd(ctx, "list")

    error_text = ctx.transport.error_text()
    assert "can't determine" in error_text.lower()
    assert "no memory entries" not in error_text.lower()
    assert not ctx.transport.system_text()


@pytest.mark.asyncio
async def test_memory_view_reads_through_the_resolved_project_root(tmp_path: Path) -> None:
    """Tier 2: /memory view <name> resolves and prints a real entry via the
    same transport-derived root."""
    workspace_dir = tmp_path / ".reyn" / "agents" / "test"
    _write_entry(
        tmp_path / ".reyn" / "memory", "user_role.md",
        name="user-role", type_="user", description="Who you are",
    )
    session = _FakeSessionWithWorkspace(workspace_dir)

    await memory_cmd(_ctx(session), "view user-role")

    text = session.reply_text()
    assert "user-role" in text
    assert "Body for user-role" in text
    assert not session.error_text()


@pytest.mark.asyncio
async def test_memory_view_not_found_reports_not_found(tmp_path: Path) -> None:
    """Tier 2: /memory view <missing> → "not found", not a stacktrace or a
    silent empty reply (the ``find_one(name)`` one-arg call this fix also
    corrects — the original always TypeError'd and fell through to a
    name-only linear scan)."""
    session = _FakeSessionWithWorkspace(tmp_path / ".reyn" / "agents" / "test")

    await memory_cmd(_ctx(session), "view nonexistent")

    assert "not found" in session.error_text().lower()
    assert not session.reply_text()
