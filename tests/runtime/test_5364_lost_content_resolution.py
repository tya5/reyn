"""Tier 2: #5364 §1.2 — a spilled entry whose backing file is gone shows an
explicit "lost" notice at wire-serialisation time, driven through the real
RouterHistoryBuffer (not the resolver called in isolation — see
tests/core/test_5364_history_content_resolve.py for that). Without this,
a spilled entry's stale read_file(path=...) preview keeps pointing at a
file that no longer exists, silently, until the model tries the read and
gets an error with no context.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY, ChatMessage
from tests._support.agent_session import make_session


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_a_spilled_entry_with_a_missing_backing_file_shows_a_lost_notice(
    tmp_path: Path,
) -> None:
    """Tier 2: build_history() on a spilled entry whose ref file was
    deleted out-of-band (simulating GC, or any other removal) returns an
    explicit lost notice, not the stale ref-preview naming a dead path."""
    session = make_session(
        agent_name="lost-content-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
    )
    ref_path = tmp_path / "gone.txt"
    # Deliberately never written — this entry is "spilled" per its own
    # meta, but its ref names a file that was never persisted / has since
    # vanished (§1.5's two `lost` reasons share this one observable shape).
    session._append_history(
        ChatMessage(
            role="tool",
            content='...[truncated: 500 chars total — full body: read_file(path="gone.txt")]...',
            ts=_now(),
            tool_call_id="tc1",
            name="tool",
            meta={SPILLED_META_KEY: True, CONTENT_REF_META_KEY: "gone.txt"},
        ),
    )
    assert not ref_path.exists(), "test setup sanity: the ref file must genuinely be absent"

    history = session._loop_driver._history_buffer.build_history()

    (tool_msg,) = [m for m in history if m.get("role") == "tool"]
    assert "read_file(path=" not in tool_msg["content"], (
        "a lost entry must not keep showing a dead read_file path"
    )
    assert "lost" in tool_msg["content"].lower()
    assert "gone.txt" in tool_msg["content"], "the lost notice should still name the missing ref"


def test_a_spilled_entry_with_its_file_present_is_unaffected(tmp_path: Path) -> None:
    """Tier 2: accept-side — a spilled entry whose backing file DOES
    exist keeps showing its normal ref-preview content unchanged."""
    session = make_session(
        agent_name="present-content-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
    )
    ref_path = tmp_path / "present.txt"
    ref_path.write_text("the real body", encoding="utf-8")
    preview = '...[truncated: 500 chars total — full body: read_file(path="present.txt")]...'
    session._append_history(
        ChatMessage(
            role="tool",
            content=preview,
            ts=_now(),
            tool_call_id="tc1",
            name="tool",
            meta={SPILLED_META_KEY: True, CONTENT_REF_META_KEY: "present.txt"},
        ),
    )

    history = session._loop_driver._history_buffer.build_history()

    (tool_msg,) = [m for m in history if m.get("role") == "tool"]
    assert tool_msg["content"] == preview, "a present file's ref-preview must pass through unchanged"
