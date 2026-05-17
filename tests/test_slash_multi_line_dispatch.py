"""Tier 2: slash dispatch must not silently drop trailing lines.

Bug context — slash commands are line-oriented today. Handlers that ignore
their args (``/cost``, ``/help``, ``/list``, ``/skills`` …) used to consume
everything after the first whitespace, including newlines, and then drop
it on the floor. The user saw their full multi-line message echoed in the
chat but no acknowledgement that the trailing lines were ignored.

Dispatch now slices ``text`` to the first line before invoking the handler
and emits a ``kind="system"`` warning when later lines carry non-whitespace
content. Whitespace-only trailing lines stay silent (no false-positive
warning on a stray trailing newline from the input widget).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _drain_outbox(session: ChatSession) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


@pytest.mark.asyncio
async def test_multi_line_slash_warns_and_dispatches_first_line(
    tmp_path, monkeypatch,
):
    """Tier 2: extra non-whitespace lines after a slash command produce a warning."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash(
        "/skill list\nthis was meant to be a question",
    )
    assert consumed is True

    msgs = _drain_outbox(session)
    systems = [m for m in msgs if m.kind == "system"]
    combined = "\n".join(m.text for m in systems)

    # A warning naming the command + "ignored extra lines" hint.
    assert "ignored extra lines" in combined
    assert "/skill" in combined
    # The original handler still ran — /skill list emits "(no active skills)"
    # when nothing is running.
    assert "no active skills" in combined.lower()


@pytest.mark.asyncio
async def test_trailing_newline_only_does_not_warn(tmp_path, monkeypatch):
    """Tier 2: ``/cmd\\n`` (trailing newline, no content) must not warn."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill list\n")
    assert consumed is True

    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs if m.kind == "system")
    assert "ignored extra lines" not in combined


@pytest.mark.asyncio
async def test_trailing_whitespace_lines_do_not_warn(tmp_path, monkeypatch):
    """Tier 2: trailing whitespace-only lines must not produce a warning."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill list\n   \n\t\n")
    assert consumed is True

    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs if m.kind == "system")
    assert "ignored extra lines" not in combined


@pytest.mark.asyncio
async def test_args_on_first_line_still_reach_handler(tmp_path, monkeypatch):
    """Tier 2: first-line args (``/cmd arg1 arg2``) still reach the handler.

    Uses ``/skill discard <id>`` because its error path is observable on the
    outbox without needing to set up a full skill run — the handler reports
    ``no running skill matches '<id>'`` when the id is unknown.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash(
        "/skill discard bogus_id\nstray line",
    )
    assert consumed is True

    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    # Warning fired …
    assert "ignored extra lines" in combined
    # … and the handler ran with "bogus_id" as the arg.
    assert "bogus_id" in combined
